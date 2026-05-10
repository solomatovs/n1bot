"""PlainTextReader — простейший `Reader[str]`: один Section со всем content'ом.

Декодирует handle целиком указанным encoding'ом и эмитит одну `Section`
с этим текстом. Никакого парсинга структуры — для этого есть
format-specific Reader'ы (`MarkdownReader`, `HtmlHeadingReader`, …).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    IncompatibleContentError,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)

__all__ = ["PlainTextReader"]


class PlainTextReader(Reader[str]):
    """`Reader[str]` который декодирует payload и эмитит один Section.

    Бросает `IncompatibleContentError` если bytes не декодируются указанным
    encoding'ом.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.text")
    DEFAULT_ENCODING: ClassVar[str] = "utf-8"
    DOC_TYPE: ClassVar[str] = "text/plain"

    def __init__(self, encoding: str = DEFAULT_ENCODING) -> None:
        self._encoding = encoding

    def name(self) -> str:
        return "PlainTextReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        raw = value.handle.read()
        try:
            text = raw.decode(self._encoding)
        except UnicodeDecodeError as e:
            raise IncompatibleContentError(
                reader_id=self.READER_ID.to_wire(),
                canonical_id=value.source_id.to_wire(),
                reason=f"cannot decode with {self._encoding!r}: {e}",
            ) from e

        yield Section(
            source_id=value.source_id,
            content=text,
            order=0,
            metadata=value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE),
        )
