"""
MarkdownReader: markdown -> поток типизированных Section-ов.

Использует MarkdownSectionParser (markdown-it-py AST -> Section'ы).
Каждый top-level markdown-блок становится конкретным Section-наследником
(HeadingSection, ParagraphSection, CodeFenceSection, TableSection,
ListSection, BlockquoteSection, HorizontalRuleSection).

Дальше pipeline применяет StructuralChunker (из boba-indexing) для
heading-prefix-merge и per-section-type chunking стратегий.
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
from boba.markdown.parser import MarkdownSectionParser

__all__ = ["MarkdownReader"]


class MarkdownReader(Reader[str]):
    """
    Reader[str] для Markdown: парсит документ в поток типизированных Section

    Каждый top-level markdown-блок (heading, paragraph, code-fence, table,
    list, blockquote, hr, raw HTML) становится соответствующим
    Section[str]-наследником с заполненным offset-tracking'ом и
    типизированными полями (HeadingSection.level/text,
    CodeFenceSection.language/code/..., TableSection.header/rows/...,
    ListSection.items/...).

    **Что меняется**:
    - source_id пробрасывается в каждую Section.
    - metadata — value.metadata.set(ReaderKeys.DOC_TYPE, "markdown").
    - anchor у HeadingSection — slug текста heading'а ("Foo Bar" ->
      "foo-bar"); у остальных секций — None.
    - order — монотонный счётчик появления секции в документе.

    **Поведение**:
    - bytes handle декодируются указанным encoding (default utf-8,
      errors="replace" — невалидные байты заменяются на U+FFFD,
      а не выбрасывают исключение).
    - Heading'и внутри code-fence (  ) игнорируются (markdown-it-py
      это умеет).
    - Inline-форматирование (**bold**, [link](),  code ) остаётся
      в content как markdown-syntax — не разворачивается.
    - Raw HTML внутри markdown (<div>...</div>) представляется как
      ParagraphSection (HTML-фрагмент в content).
    """

    DOC_TYPE: ClassVar[str] = "markdown"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.markdown")
    DEFAULT_ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, encoding: str = DEFAULT_ENCODING) -> None:
        self._parser = MarkdownSectionParser()
        self._encoding = encoding

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        text = value.handle.read().decode(self._encoding, errors="replace")
        if not text:
            return

        base_metadata = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)

        yield from self._parser.parse(
            text,
            source_id=value.source_id,
            base_metadata=base_metadata,
        )
