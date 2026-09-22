"""Потоковость обхода: спейсы с сотнями pdf и картинок под OCR. liteparse за первую
сотню разборов набирает свои кэши и выходит на плато, поэтому сравниваются два спейса
за плато: пик RSS процесса не растёт с объёмом спейса, а RSS теста-родителя — с числом
спейсов. Всё, что обход набирает, живёт один объект за раз и умирает вместе с
процессом.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

import asyncio
import io
import multiprocessing
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor

import pytest
from cfl_stand import PACKAGE_DIR, StubIndexer
from PIL import Image, ImageDraw

from boba.cfl_indexer.worker import IndexerConfig, Report, index_space
from boba.stand.confluence import ConfluenceStub, StubAttachment, StubPage, StubSpace
from boba.stand.ix import IxStand, peak_rss_mib
from boba.text.document import LiteParseParams

pytestmark = [pytest.mark.load, pytest.mark.anyio]

MEDIUM_PAGES = 120
LARGE_PAGES = 240
MANY_SPACES = 6
MANY_PAGES = 20
RSS_SLACK_MIB = 48
GROWTH_RATIO = 1.25


def make_png(text: str) -> bytes:
    image = Image.new("RGB", (640, 120), "white")
    ImageDraw.Draw(image).text((12, 44), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return buffer.getvalue()


def make_pdf(text: str) -> bytes:
    """Одностраничный pdf с текстовым слоем; смещения xref считаются честно."""
    stream = f"BT /F1 18 Tf 40 700 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()

    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()

    return bytes(out)


def seed_space(stub: ConfluenceStub, key: str, pages: int) -> None:
    """Страницы с pdf и png; байты файлов свои у каждой страницы, как на сервере."""
    stub.describe(StubSpace(key=key, name=f"Space {key}"))
    for number in range(pages):
        page_id = f"{key}-{number}"
        page = StubPage(
            id=page_id,
            space=key,
            title=f"Page {number} of {key}",
            html=f"<h1>Page {number}</h1><p>Body of page {number} in {key}.</p>",
        )
        page.attachments.append(
            StubAttachment(
                id=f"{page_id}-pdf",
                title=f"doc-{number}.pdf",
                media_type="application/pdf",
                content=make_pdf(f"Document {number} of space {key}"),
            )
        )
        page.attachments.append(
            StubAttachment(
                id=f"{page_id}-png",
                title=f"pic-{number}.png",
                media_type="image/png",
                content=make_png(f"Picture {number} of space {key}"),
            )
        )
        stub.add(page)


def rss_mib() -> int:
    with open("/proc/self/statm", encoding="ascii") as file:
        fields = file.read().split()

    return (int(fields[1]) * 4096) >> 20


def index_in_child(cfg: IndexerConfig, key: str) -> tuple[Report, int]:
    """Вход процесса спейса: отчёт обхода и пик RSS процесса в MiB."""
    report = index_space(cfg, cfg.sources[0].name, key, PACKAGE_DIR / "run", False)

    return report, peak_rss_mib()


def index_apart(
    cfg: IndexerConfig, keys: Sequence[str]
) -> dict[str, tuple[Report, int]]:
    """Каждый спейс в свежем процессе, parallel_spaces разом; заглушку Confluence
    обслуживает loop теста, поэтому зовётся из потока."""
    with ProcessPoolExecutor(
        max_workers=cfg.parallel_spaces,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        futures = {key: pool.submit(index_in_child, cfg, key) for key in keys}
        results: dict[str, tuple[Report, int]] = {}
        for key, future in futures.items():
            results[key] = future.result()

    return results


def ocr_config(
    stub_indexer: StubIndexer, ix_stand: IxStand, *spaces: str
) -> IndexerConfig:
    parser = LiteParseParams(
        ocr_enabled=True, ocr_language="eng", tessdata_path=ix_stand.tessdata_path
    )
    cfg = stub_indexer.config(*spaces)

    return cfg.model_copy(
        update={"parser": parser, "parallel_spaces": 2, "progress_every": 50}
    )


class TestStreamingMemory:
    async def test_peak_rss_does_not_grow_with_space_size(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_stand: IxStand,
    ) -> None:
        fake, _ = stub
        seed_space(fake, "MEDIUM", MEDIUM_PAGES)
        seed_space(fake, "LARGE", LARGE_PAGES)
        cfg = ocr_config(stub_indexer, ix_stand, "MEDIUM", "LARGE")

        results = await asyncio.to_thread(index_apart, cfg, ["MEDIUM", "LARGE"])

        medium, medium_peak = results["MEDIUM"]
        large, large_peak = results["LARGE"]
        assert medium.ok, medium.line()
        assert large.ok, large.line()
        assert medium.seen == 1 + MEDIUM_PAGES * 3
        assert large.seen == 1 + LARGE_PAGES * 3
        assert large_peak <= medium_peak * GROWTH_RATIO + RSS_SLACK_MIB, (
            f"large space peaked at {large_peak} MiB against "
            f"{medium_peak} MiB for the medium one"
        )

    async def test_parent_stays_flat_across_many_spaces(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_stand: IxStand,
    ) -> None:
        fake, _ = stub
        keys: list[str] = []
        for number in range(MANY_SPACES):
            key = f"SP{number}"
            keys.append(key)
            seed_space(fake, key, MANY_PAGES)

        cfg = ocr_config(stub_indexer, ix_stand, *keys)
        before = rss_mib()

        results = await asyncio.to_thread(index_apart, cfg, keys)

        after = rss_mib()
        peaks: list[int] = []
        for report, peak in results.values():
            assert report.ok, report.line()
            assert report.seen == 1 + MANY_PAGES * 3
            peaks.append(peak)

        assert max(peaks) <= min(peaks) * GROWTH_RATIO + RSS_SLACK_MIB, peaks
        assert after - before <= RSS_SLACK_MIB, f"parent grew {before} -> {after} MiB"
