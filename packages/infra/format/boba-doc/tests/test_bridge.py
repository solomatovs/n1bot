"""AsyncPipe: async-чанки в роутер через пипу, ошибки источника и потребителя."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import BinaryIO

import pytest
from samples import Samples

from boba.doc.bridge import AsyncPipe
from boba.doc.config import DisabledOcrConfig, OcrLanguage, RapidOcrConfig
from boba.doc.document import DocumentError, DocumentHint, PageWindow
from boba.doc.ocr import OcrEngines
from boba.doc.router import DocumentRouter
from boba.llm.providers import LlmProviders, LlmProviderTypes

pytestmark = pytest.mark.anyio

PDF_HINT = DocumentHint(media_type="application/pdf")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


async def chunked(data: bytes, size: int) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


class SourceDropError(Exception):
    """Источник оборвался посреди тела."""


async def failing(data: bytes) -> AsyncIterator[bytes]:
    yield data[: len(data) // 2]
    raise SourceDropError("connection dropped")


def read_all(router: DocumentRouter, hint: DocumentHint):
    def consume(source: BinaryIO) -> list[str]:
        with router.open(source, hint) as document:
            return [page.text for page in document.pages(PageWindow.whole())]

    return consume


async def test_pdf_arrives_through_pipe_in_small_chunks(router: DocumentRouter) -> None:
    data = Samples.pdf(["alpha", "beta"])

    texts = await AsyncPipe.run(chunked(data, 7), read_all(router, PDF_HINT))

    assert [text.strip() for text in texts] == ["alpha", "beta"]


async def test_large_body_does_not_deadlock(router: DocumentRouter) -> None:
    """Тело больше ёмкости пипы: писатель ждёт drain, потребитель читает."""
    lines = [f"line {index}" for index in range(200_000)]
    data = "\n".join(lines).encode()
    hint = DocumentHint(media_type="text/plain")

    texts = await AsyncPipe.run(chunked(data, 1 << 16), read_all(router, hint))

    assert texts[0].endswith("line 199999")


async def test_source_error_wins_with_reader_note(
    router: DocumentRouter,
) -> None:
    data = Samples.pdf(["broken"])

    with pytest.raises(SourceDropError) as caught:
        await AsyncPipe.run(failing(data), read_all(router, PDF_HINT))

    notes = getattr(caught.value, "__notes__", [])
    assert any("reader failed" in note for note in notes)


async def test_reader_failure_propagates(router: DocumentRouter) -> None:
    with pytest.raises(DocumentError, match="pdf document: opening failed"):
        await AsyncPipe.run(chunked(b"%PDF-1.4 garbage", 4), read_all(router, PDF_HINT))


def test_ocr_engines_factory(doc_stand) -> None:
    engines = OcrEngines(LlmProviders(LlmProviderTypes.installed()))
    disabled = engines.of(DisabledOcrConfig(provider="off"))
    assert disabled.recognize(Samples.image(["x"], doc_stand.cyrillic_font)) == ""

    config = RapidOcrConfig(
        provider="rapidocr",
        models_dir=doc_stand.ocr_models_dir,
        language=OcrLanguage.ESLAV,
        text_score=0.5,
        threads=1,
    )
    assert config.enabled is True
    assert config.fingerprint() == {
        "provider": "rapidocr",
        "language": "eslav",
        "text_score": 0.5,
    }
    assert DisabledOcrConfig(provider="off").fingerprint() == {"provider": "off"}
