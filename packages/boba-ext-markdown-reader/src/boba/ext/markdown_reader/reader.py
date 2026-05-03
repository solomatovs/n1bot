"""MarkdownReader: v0.1 — markdown как plain text. Heading-aware — позже."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    IndexingContext,
    Reader,
    ReaderId,
    Section,
    SourceItem,
)

__all__ = ["MarkdownReader"]


_HINTS = frozenset({"md", "markdown"})


class MarkdownReader(Reader):
    def name(self) -> str:
        return "MarkdownReader"

    def reader_id(self) -> ReaderId:
        return ReaderId("ext.markdown")

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
            metadata={**value.metadata, "format": "markdown"},
        )
