"""TextReader: plain UTF-8 → одна Section на документ."""

from __future__ import annotations

from collections.abc import Iterable

from boba.processing import (
    IndexingContext,
    RawDocument,
    Reader,
    ReaderId,
    Section,
)

__all__ = ["TextReader"]


class TextReader(Reader):
    """Декодирует handle как UTF-8 и выдаёт одну Section."""

    def name(self) -> str:
        return "TextReader"

    def reader_id(self) -> ReaderId:
        return ReaderId("ext.text")

    def convert(
        self, ctx: IndexingContext, value: RawDocument
    ) -> Iterable[Section]:
        del ctx
        text = value.handle.read().decode("utf-8", errors="replace")
        if not text.strip():
            return
        yield Section(
            source_id=value.source_id,
            text=text,
            metadata={**value.metadata, "format": "text"},
        )
