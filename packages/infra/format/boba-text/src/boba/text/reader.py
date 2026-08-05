"""PlainTextReader — простейший Reader[str]: один Section со всем content'ом.

Декодирует handle целиком первой подошедшей кодировкой из списка и эмитит
одну Section с этим текстом. Никакого парсинга структуры — для этого есть
format-specific Reader'ы (MarkdownReader, HtmlHeadingReader, …).

TextMedia — карта текстовых форматов, которые liteparse не читает вовсе
(.txt, .md он отвергает с `unsupported file format`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import ClassVar

from boba.indexing import (
    IncompatibleContentError,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)

__all__ = ["PlainTextReader", "TextMedia"]


class PlainTextReader(Reader[str]):
    """Reader[str] который декодирует payload и эмитит один Section.

    Кодировки перебираются по порядку; если не подошла ни одна —
    IncompatibleContentError.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.text")

    def __init__(self, *, doc_type: str, encodings: Sequence[str]) -> None:
        if not encodings:
            msg = "PlainTextReader: encodings must be non-empty"
            raise ValueError(msg)
        self._doc_type = doc_type
        self._encodings = tuple(encodings)

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        raw = value.handle.read()
        yield Section(
            source_id=value.source_id,
            content=self._decode(raw, value),
            order=0,
            metadata=value.metadata.set(ReaderKeys.DOC_TYPE, self._doc_type),
        )

    def _decode(self, raw: bytes, value: RawDocument) -> str:
        for encoding in self._encodings:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        tried = ", ".join(self._encodings)
        raise IncompatibleContentError(
            reader_id=self.READER_ID,
            canonical_id=value.source_id,
            reason=f"cannot decode with any of: {tried}",
        )


class TextMedia:
    """media_type -> doc_type: текстовые форматы мимо liteparse."""

    DOC_TYPE_BY_MEDIA_TYPE: ClassVar[Mapping[str, str]] = {
        "text/plain": "txt",
        "text/markdown": "md",
        "text/x-markdown": "md",
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
    }

    @classmethod
    def readers(cls, encodings: Sequence[str]) -> dict[str, PlainTextReader]:
        """Роуты DispatchReader'а: свой PlainTextReader на каждый media_type."""
        routes: dict[str, PlainTextReader] = {}
        for media_type, doc_type in cls.DOC_TYPE_BY_MEDIA_TYPE.items():
            routes[media_type] = PlainTextReader(
                doc_type=doc_type,
                encodings=encodings,
            )
        return routes
