"""Потоковость обхода: спейсы с сотнями pdf и картинок под OCR. Сравниваются два
спейса разного объёма: пик RSS процесса не растёт с объёмом спейса, а RSS
теста-родителя — с числом спейсов. Всё, что обход набирает, живёт один объект за
раз и умирает вместе с процессом.

Ошибки стенда: IxStandError — секции [ix_stand] нет, DocStandError — секции
    [doc_stand] нет; модуль пропускается.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from cfl_stand import PACKAGE_DIR, StubIndexer
from PIL import Image, ImageDraw

from boba.cfl_indexer.worker import IndexerConfig, Report, run_spaces
from boba.doc.ocr import OcrLanguage, RapidOcrConfig
from boba.stand.confluence import ConfluenceStub, StubAttachment, StubPage, StubSpace
from boba.stand.doc import DocStand

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


def ocr_config(
    stub_indexer: StubIndexer, doc_stand: DocStand, *spaces: str
) -> IndexerConfig:
    ocr = RapidOcrConfig(
        provider="rapidocr",
        models_dir=doc_stand.ocr_models_dir,
        language=OcrLanguage.EN,
        text_score=0.5,
        threads=1,
    )
    cfg = stub_indexer.config(*spaces)
    doc = cfg.doc.model_copy(update={"ocr": ocr})

    return cfg.model_copy(
        update={"doc": doc, "parallel_spaces": 2, "progress_every": 50}
    )


class TestStreamingMemory:
    async def test_peak_rss_does_not_grow_with_space_size(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        doc_stand: DocStand,
    ) -> None:
        fake, _ = stub
        seed_space(fake, "MEDIUM", MEDIUM_PAGES)
        seed_space(fake, "LARGE", LARGE_PAGES)
        cfg = ocr_config(stub_indexer, doc_stand, "MEDIUM", "LARGE")

        reports = await asyncio.to_thread(run_spaces, cfg, PACKAGE_DIR / "run")

        by_key: dict[str, Report] = {}
        for report in reports:
            by_key[report.space_key] = report

        medium = by_key["MEDIUM"]
        large = by_key["LARGE"]
        assert medium.ok, medium.line()
        assert large.ok, large.line()
        assert medium.seen == 1 + MEDIUM_PAGES * 3
        assert large.seen == 1 + LARGE_PAGES * 3
        assert (
            large.peak_rss_mib <= medium.peak_rss_mib * GROWTH_RATIO + RSS_SLACK_MIB
        ), (
            f"large space peaked at {large.peak_rss_mib} MiB against "
            f"{medium.peak_rss_mib} MiB for the medium one"
        )

    async def test_parent_stays_flat_across_many_spaces(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        doc_stand: DocStand,
    ) -> None:
        fake, _ = stub
        keys: list[str] = []
        for number in range(MANY_SPACES):
            key = f"SP{number}"
            keys.append(key)
            seed_space(fake, key, MANY_PAGES)

        cfg = ocr_config(stub_indexer, doc_stand, *keys)
        before = rss_mib()

        reports = await asyncio.to_thread(run_spaces, cfg, PACKAGE_DIR / "run")

        after = rss_mib()
        peaks: list[int] = []
        for report in reports:
            assert report.ok, report.line()
            assert report.seen == 1 + MANY_PAGES * 3
            peaks.append(report.peak_rss_mib)

        assert max(peaks) <= min(peaks) * GROWTH_RATIO + RSS_SLACK_MIB, peaks
        assert after - before <= RSS_SLACK_MIB, f"parent grew {before} -> {after} MiB"
