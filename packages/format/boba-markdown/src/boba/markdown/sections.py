"""Markdown-специфичные Section-типы.

Это format-specific расширения доменной иерархии `boba.indexing.Section`.
Их структура (плоская 2D-таблица, плоский список, code-fence с language hint)
отражает GFM-разметку и не предназначена быть универсальной — другим
форматам (HTML, PDF, Jupyter) лучше иметь свои собственные Section-типы
с честной семантикой формата.

Каждый тип несёт per-unit `*_locations` для locality-aware chunking
(table-row split, list-item split, code-line split).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.indexing import (
    ChunkLocation,
    Metadata,
    Section,
)
from boba.markdown.keys import MarkdownKeys

__all__ = [
    "MarkdownBlockquoteSection",
    "MarkdownCodeFenceSection",
    "MarkdownHorizontalRuleSection",
    "MarkdownListSection",
    "MarkdownTableSection",
]


@dataclass(frozen=True)
class MarkdownCodeFenceSection(Section[str]):
    """Markdown code-fence (` ```lang ... ``` `).

    - `language`            — language hint (`None` если не указан).
    - `code`                — содержимое БЕЗ обрамляющих маркеров.
    - `code_line_locations` — char-offset каждой строки тела для line-based
                              split при overflow большого блока кода.
    """

    SECTION_TYPE: ClassVar[str] = "markdown.code_fence"

    language: str | None = None
    code: str = ""
    code_line_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.language:
            m = m.set(MarkdownKeys.CODE_LANGUAGE, self.language)
        return m


@dataclass(frozen=True)
class MarkdownTableSection(Section[str]):
    """Markdown GFM-таблица.

    Плоская 2D-структура — markdown-специфичная shape (без colspan / nested-cells
    / thead-tbody-tfoot, которые есть в HTML).

    - `header`           — distinct cells header'а (распарсенный inline-text).
    - `rows`             — distinct cells каждой data-строки.
    - `header_text`      — raw текст header'а вместе с разметкой-разделителем
                            (`| col |\\n|---|`). Для chunker'а: реплицируется
                            в `format_content` row-by-row split'а, чтобы LLM
                            видела имена столбцов.
    - `header_location`  — char-offset header_text в исходнике.
    - `row_locations`    — char-offset каждой data-строки.
    """

    SECTION_TYPE: ClassVar[str] = "markdown.table"

    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    header_text: str = ""
    header_location: ChunkLocation | None = None
    row_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.header_text:
            m = m.set(MarkdownKeys.TABLE_HEADER, self.header_text)
        return m


@dataclass(frozen=True)
class MarkdownListSection(Section[str]):
    """Markdown ordered/unordered список.

    Плоская проекция — top-level items без сохранения вложенности (вложенный
    список инлайнится в текст items'а как есть).

    - `items`           — извлечённый текст каждого top-level item.
    - `item_locations`  — char-offset каждого item для item-by-item split'а.
    """

    SECTION_TYPE: ClassVar[str] = "markdown.list"

    ordered: bool = False
    items: tuple[str, ...] = ()
    item_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        return Metadata.empty().set(MarkdownKeys.LIST_ORDERED, self.ordered)


@dataclass(frozen=True)
class MarkdownBlockquoteSection(Section[str]):
    """Markdown цитата (`> ...`)."""

    SECTION_TYPE: ClassVar[str] = "markdown.blockquote"


@dataclass(frozen=True)
class MarkdownHorizontalRuleSection(Section[str]):
    """Markdown горизонтальный разделитель (`---`).

    Структурный маркёр, не контентная единица — chunker'у обычно стоит
    пропускать такие секции (не эмитить их в Chunk'и).
    """

    SECTION_TYPE: ClassVar[str] = "markdown.horizontal_rule"
