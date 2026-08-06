"""Тесты PlainTextReader: перебор кодировок и doc_type роута."""

from __future__ import annotations

import pytest

from boba.indexing import (
    ChunkStream,
    IncompatibleContentError,
    Metadata,
    RawDocument,
    ReaderKeys,
    SourceId,
)
from boba.text import PlainTextReader, TextMedia

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"

_SOURCE = "https://confl/download/attachments/42/notes.md"


def _raw(data: bytes) -> RawDocument:
    return RawDocument(
        handle=ChunkStream.of(data),
        source_id=SourceId(_SOURCE),
        metadata=Metadata.empty(),
    )


class TestPlainTextReader:
    async def test_utf8_one_section_with_doc_type(self) -> None:
        reader = PlainTextReader(doc_type="md", encodings=["utf-8"])
        secs = [
            item async for item in reader.read(_raw("# Заголовок\n\nТекст.".encode()))
        ]
        assert len(secs) == 1
        assert secs[0].order == 0
        assert secs[0].content == "# Заголовок\n\nТекст."
        assert secs[0].metadata.get(ReaderKeys.DOC_TYPE) == "md"

    async def test_falls_through_to_next_encoding(self) -> None:
        reader = PlainTextReader(doc_type="txt", encodings=["utf-8", "cp1251"])
        secs = [item async for item in reader.read(_raw("Привет".encode("cp1251")))]
        assert secs[0].content == "Привет"

    async def test_first_matching_encoding_wins(self) -> None:
        reader = PlainTextReader(doc_type="txt", encodings=["cp1251", "utf-8"])
        # utf-8-байты читаемы и как cp1251 — выигрывает первая кодировка списка
        secs = [item async for item in reader.read(_raw("Привет".encode()))]
        assert secs[0].content != "Привет"

    async def test_no_encoding_matched_is_incompatible(self) -> None:
        reader = PlainTextReader(doc_type="txt", encodings=["utf-8", "ascii"])
        with pytest.raises(IncompatibleContentError, match="utf-8, ascii"):
            [item async for item in reader.read(_raw(b"\xff\xfe\x00\x01"))]

    def test_empty_encodings_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            PlainTextReader(doc_type="txt", encodings=[])


class TestTextMedia:
    def test_routes_cover_text_media_types(self) -> None:
        routes = TextMedia.readers(["utf-8"])
        assert set(routes) == set(TextMedia.DOC_TYPE_BY_MEDIA_TYPE)

    async def test_route_carries_its_doc_type(self) -> None:
        routes = TextMedia.readers(["utf-8"])
        secs = [item async for item in routes["text/markdown"].read(_raw(b"# h"))]
        assert secs[0].metadata.get(ReaderKeys.DOC_TYPE) == "md"
