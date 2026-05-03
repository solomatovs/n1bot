"""TextReader: plain UTF-8 → одна Section на документ."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    IndexingContext,
    Reader,
    ReaderId,
    Section,
    SourceItem,
)

__all__ = ["TextReader"]


_HINTS = frozenset({"txt", "text", "log"})


class TextReader(Reader):
    """Декодирует payload как UTF-8 и выдаёт одну Section."""

    def name(self) -> str:
        return "TextReader"

    def reader_id(self) -> ReaderId:
        return ReaderId("ext.text")

    def accepts(self, item: SourceItem) -> bool:
        return item.content_hint in _HINTS

    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]:
        del ctx
        text = value.payload.decode("utf-8", errors="replace")
        if not text.strip():
            return
        yield Section(
            source_id=value.source_id,
            text=text,
            content_hash=value.content_hash,
            metadata={**value.metadata, "format": "text"},
        )
