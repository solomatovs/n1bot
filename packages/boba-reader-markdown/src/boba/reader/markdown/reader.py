"""MarkdownReader: markdown → heading-aware Section[str].

Каждый ATX-heading (`# A`, `## B`, ...) → отдельная Section с anchor'ом
из slug текста (или fallback `idx:N`). Текст Section — heading-строка +
body до следующего heading'а.

Если в документе нет heading'ов — fallback одной Section со всем body.
Preamble до первого heading'а становится отдельной anchor-less Section.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)
from boba.reader.markdown.keys import MarkdownKeys
from boba.reader.markdown.parser import MarkdownSection, anchor_for, split_sections

__all__ = ["MarkdownReader"]


class MarkdownReader(Reader[str]):
    """Reader[str] для markdown с ATX heading'ами."""

    DOC_TYPE: ClassVar[str] = "markdown"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.markdown")

    def name(self) -> str:
        return "MarkdownReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        text = value.handle.read().decode("utf-8", errors="replace")
        for md_sec in split_sections(text):
            section = self._build_section(value, md_sec)
            if section is not None:
                yield section

    def _build_section(
        self, value: RawDocument, md_sec: MarkdownSection
    ) -> Section[str] | None:
        h = md_sec.heading
        if h is None:
            body = md_sec.body.strip()
            if not body:
                return None
            return Section(
                source_id=value.source_id,
                content=body,
                anchor=None,
                order=0,
                metadata=value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE),
            )
        heading_md = "#" * h.level + " " + h.text
        body = md_sec.body.strip()
        text = heading_md + (("\n\n" + body) if body else "")
        return Section(
            source_id=value.source_id,
            content=text,
            anchor=anchor_for(h),
            order=h.index,
            metadata=(
                value.metadata
                .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
                .set(MarkdownKeys.HEADING_LEVEL, h.level)
                .set(MarkdownKeys.HEADING_TEXT, h.text)
            ),
        )
