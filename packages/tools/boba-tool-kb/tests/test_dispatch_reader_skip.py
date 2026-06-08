"""DispatchReader.on_unknown: skip-режим для multi-format потоков.

Проверяет, что в Confluence-ingest-сценарии (HTML страницы + произвольные
attachment'ы) attachment-документы с непокрытым CONTENT_TYPE не валят
весь pipeline, а молча пропускаются (yield пустой). Заодно регрессионно
страхует default-режим error — чтобы случайный пропуск ключа не остался
незамеченным в других pipeline'ах.
"""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO

import pytest

from boba.indexing import (
    Metadata,
    RawDocument,
    Section,
    SourceId,
    TransportKeys,
)
from boba.indexing.dispatch_reader import DispatchReader
from boba.indexing.errors import IncompatibleContentError
from boba.indexing.reader import Reader, ReaderId


class _MarkerReader(Reader[str]):
    """Sub-Reader, который yield'ит один Section с фиксированной меткой."""

    def __init__(self, marker: str) -> None:
        self._marker = marker

    def reader_id(self) -> ReaderId:
        return ReaderId(f"ext.test.marker.{self._marker}")

    def read(self, raw: RawDocument) -> Iterable[Section[str]]:
        yield Section(
            source_id=raw.source_id,
            metadata=raw.metadata,
            content=self._marker,
        )


def _doc_with_content_type(ct: str | None) -> RawDocument:
    meta = Metadata.empty()
    if ct is not None:
        meta = meta.set(TransportKeys.CONTENT_TYPE, ct)
    return RawDocument(
        handle=BytesIO(b""),
        source_id=SourceId(f"test://{ct or 'no-type'}"),
        metadata=meta,
    )


def test_skip_unknown_content_type_yields_nothing() -> None:
    """image/png без route -> пустой Iterable, без исключения."""
    reader = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={"text/html": _MarkerReader("html")},
        reader_id=ReaderId("ext.test.dispatch"),
        on_unknown="skip",
    )
    out = list(reader.read(_doc_with_content_type("image/png")))
    assert out == []


def test_skip_missing_key_yields_nothing() -> None:
    """Метадата вообще без CONTENT_TYPE — тоже skip, не error."""
    reader = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={"text/html": _MarkerReader("html")},
        reader_id=ReaderId("ext.test.dispatch"),
        on_unknown="skip",
    )
    out = list(reader.read(_doc_with_content_type(None)))
    assert out == []


def test_skip_does_not_break_known_routes() -> None:
    """skip-режим не ломает honest роутинг: known-формат всё ещё попадает в sub-Reader."""
    reader = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={
            "text/html": _MarkerReader("html"),
            "application/pdf": _MarkerReader("pdf"),
        },
        reader_id=ReaderId("ext.test.dispatch"),
        on_unknown="skip",
    )
    html_out = list(reader.read(_doc_with_content_type("text/html")))
    pdf_out = list(reader.read(_doc_with_content_type("application/pdf")))
    assert [s.content for s in html_out] == ["html"]
    assert [s.content for s in pdf_out] == ["pdf"]


def test_error_mode_still_raises_on_unknown() -> None:
    """Default-mode (error) должен по-прежнему бросать IncompatibleContentError."""
    reader = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={"text/html": _MarkerReader("html")},
        reader_id=ReaderId("ext.test.dispatch"),
    )
    with pytest.raises(IncompatibleContentError):
        list(reader.read(_doc_with_content_type("image/png")))


def test_error_mode_raises_on_missing_key() -> None:
    reader = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={"text/html": _MarkerReader("html")},
        reader_id=ReaderId("ext.test.dispatch"),
    )
    with pytest.raises(IncompatibleContentError):
        list(reader.read(_doc_with_content_type(None)))
