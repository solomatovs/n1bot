"""Тесты LiteParseReader: бинарь -> Section на страницу + метаданные."""

from __future__ import annotations

import pytest

from boba.indexing import (
    ChunkStream,
    IncompatibleContentError,
    Metadata,
    RawDocument,
    ReaderKeys,
    SectionKeys,
    SourceId,
    TransportKeys,
)
from boba.liteparse.engine import LiteParseReader
from boba.text.document import DocumentMedia, LiteParseParams

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"

# Двухстраничный PDF: стр.1 "Alpha page one", стр.2 "Beta page two".
_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""

_SOURCE = "https://confl/download/attachments/42/report.pdf"

_PDF_TYPE = "application/pdf"

_PARAMS = LiteParseParams(ocr_enabled=False, tessdata_path="/usr/share/tessdata")


def _raw(data: bytes, content_type: str | None) -> RawDocument:
    # пробрасываемая upstream-метадата вложения (как ставит make_attachment_request)
    meta = Metadata.empty().set(ReaderKeys.PAGE_TITLE, "report.pdf")
    if content_type is not None:
        meta = meta.set(TransportKeys.CONTENT_TYPE, content_type)
    return RawDocument(
        handle=ChunkStream.of(data),
        source_id=SourceId(_SOURCE),
        metadata=meta,
    )


async def test_pdf_one_section_per_page_with_locus() -> None:
    secs = [
        item
        async for item in LiteParseReader(_PARAMS).read(_raw(_PDF, _PDF_TYPE))
    ]
    assert [s.order for s in secs] == [1, 2]
    assert [s.metadata.get(SectionKeys.PAGE_NUMBER) for s in secs] == [1, 2]
    assert all(s.metadata.get(ReaderKeys.DOC_TYPE) == "pdf" for s in secs)
    assert "Alpha page one" in secs[0].content
    assert "Beta page two" in secs[1].content


async def test_passes_through_source_metadata() -> None:
    [first, *_] = [
        item
        async for item in LiteParseReader(_PARAMS).read(_raw(_PDF, _PDF_TYPE))
    ]
    assert first.source_id == SourceId(_SOURCE)
    # имя файла (page_title), выставленное upstream, доезжает до Section
    assert first.metadata.get(ReaderKeys.PAGE_TITLE) == "report.pdf"


async def test_content_type_with_params_is_normalized() -> None:
    raw = _raw(_PDF, "Application/PDF; charset=binary")
    secs = [item async for item in LiteParseReader(_PARAMS).read(raw)]
    assert [s.order for s in secs] == [1, 2]


async def test_unsupported_content_type_raises_incompatible() -> None:
    with pytest.raises(IncompatibleContentError):
        [item async for item in LiteParseReader(_PARAMS).read(_raw(_PDF, "image/png"))]


async def test_missing_content_type_raises_incompatible() -> None:
    with pytest.raises(IncompatibleContentError):
        [item async for item in LiteParseReader(_PARAMS).read(_raw(_PDF, None))]


async def test_corrupt_document_raises_incompatible() -> None:
    stream = LiteParseReader(_PARAMS).read(_raw(b"not a real pdf", _PDF_TYPE))

    with pytest.raises(IncompatibleContentError):
        [item async for item in stream]


async def test_empty_payload_yields_nothing() -> None:
    empty = LiteParseReader(_PARAMS).read(_raw(b"", "application/pdf"))
    assert [section async for section in empty] == []


def test_media_types_match_default_suffix_map() -> None:
    reader = LiteParseReader(_PARAMS)
    assert "application/pdf" in reader.media_types
    assert set(reader.media_types) == set(DocumentMedia.SUFFIX_BY_MEDIA_TYPE)
