"""MarkdownReader: markdown → heading-aware Section'ы.

Каждый ATX-heading (`# A`, `## B`, ...) → отдельная Section с anchor'ом
из slug текста (или fallback `idx:N`). Текст Section — heading + body
до следующего heading'а.

Если в документе нет heading'ов — fallback одной Section со всем body.
Preamble до первого heading'а становится отдельной anchor-less Section.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.markdown_parser import (
    Section as MdSection,
)
from boba.ext.markdown_parser import (
    anchor_for,
    split_sections,
)
from boba.indexing import (
    IndexingContext,
    Reader,
    ReaderId,
    Section,
    SourceItem,
)

__all__ = ["MarkdownReader"]


_HINTS = frozenset({"md", "markdown"})
_FORMAT = "markdown"


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
        for md_sec in split_sections(text):
            section = self._build_section(value, md_sec)
            if section is not None:
                yield section

    def _build_section(
        self, value: SourceItem, md_sec: MdSection
    ) -> Section | None:
        h = md_sec.heading
        if h is None:
            body = md_sec.body.strip()
            if not body:
                return None
            return Section(
                source_id=value.source_id,
                text=body,
                anchor=None,
                order=0,
                content_hash=value.content_hash,
                metadata={**value.metadata, "format": _FORMAT},
            )
        heading_md = "#" * h.level + " " + h.text
        body = md_sec.body.strip()
        text = heading_md + (("\n\n" + body) if body else "")
        return Section(
            source_id=value.source_id,
            text=text,
            anchor=anchor_for(h),
            order=h.index,
            content_hash=value.content_hash,
            metadata={
                **value.metadata,
                "format": _FORMAT,
                "heading_level": str(h.level),
                "heading_text": h.text,
            },
        )
