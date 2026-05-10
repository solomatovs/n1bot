"""
MarkdownReader: markdown → heading-aware Section[str]

Каждый ATX-heading (`# A`, `## B`, ...) → отдельная Section с anchor из slug текста.
Section = heading-строка + body до следующего heading

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
    """
    `Reader[str]` для Markdown: режет документ на Section'и по ATX-heading'ам.

    **Схема**:
    ```markdown
    preamble before any heading

    # Intro
    intro body

    ## API
    api body
    ```
    ```python
    ──reader.convert(raw)──→
    Section(content="preamble before any heading", anchor=None, order=0,
            metadata={DOC_TYPE: "markdown"})
    Section(content="# Intro\\n\\nintro body", anchor="intro", order=1,
            metadata={DOC_TYPE: "markdown", HEADING_LEVEL: 1, HEADING_TEXT: "Intro"})
    Section(content="## API\\n\\napi body",   anchor="api",   order=2,
            metadata={DOC_TYPE: "markdown", HEADING_LEVEL: 2, HEADING_TEXT: "API"})
    ```

    **Поведение**:
    - ATX-heading'и (`#`, `##`, …); внутри code-fence (` ``` `) heading'и
      игнорируются.
    - `anchor` — slug текста heading'а (`"Foo Bar! 123"` → `"foo-bar-123"`);
      fallback `"idx:N"` если slug пуст.
    - Preamble до первого heading'а — отдельная anchor-less Section
      (если непуст).
    - Без heading'ов — одна Section со всем body, `anchor=None`.
    - bytes декодируются как UTF-8 с `errors="replace"`.

    **Пример**:
    ```python
    reader = MarkdownReader()
    raw = RawDocument(
        handle=BytesIO(
            b"preamble before any heading\\n\\n"
            b"# Intro\\nintro body\\n\\n"
            b"## API\\napi body"
        ),
        source_id=SourceId("doc1"),
    )

    # preamble → отдельная anchor-less Section; затем по Section на каждый heading.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),                  # pass из RawDocument
            content="preamble before any heading",       # новое: текст до первого heading'а
            anchor=None,                                 # новое: None у preamble (нет heading'а)
            order=0,                                     # новое: позиция в документе
            metadata=Metadata.empty().set(ReaderKeys.DOC_TYPE, "markdown"),  # merge: + DOC_TYPE
        ),
        Section(
            source_id=SourceId("doc1"),
            content="# Intro\\n\\nintro body",
            anchor="intro",                              # новое: slug("Intro") → "intro"
            order=1,
            metadata=(                                   # merge: + DOC_TYPE / HEADING_*
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 1)
                .set(MarkdownKeys.HEADING_TEXT, "Intro")
            ),
        ),
        Section(
            source_id=SourceId("doc1"),
            content="## API\\n\\napi body",
            anchor="api",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 2)
                .set(MarkdownKeys.HEADING_TEXT, "API")
            ),
        ),
    ]
    ```
    """  # noqa: E501

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
