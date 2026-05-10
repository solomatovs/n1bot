"""
Section[T] — логический фрагмент документа.

Базовый `Section[T]` плюс открытая иерархия типизированных подклассов:
- HeadingSection
- ParagraphSection
- CodeFenceSection
- TableSection
- ListSection,
- BlockquoteSection
- HorizontalRuleSection

Доменно-нейтральная — format Reader выдаёт эти типы.
Можно добавить свои подклассы (`ConfluenceMacroSection`, `JupyterCellSection`, ...).

Контракт:

- `location` — char/byte-offset в исходном raw документе (после декодирования).
  Сохраняется на каждом Section, пробрасывается в Chunk и используется LLM/UI
  для цитирования

- `to_chunk_metadata()` — переопределяется в подклассах для эмиссии типизированных
  полей в `chunk.metadata` через `MetadataKey`

- `SECTION_TYPE` — canonical alias короткий идентификатор для wire-сериализации
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from boba.indexing.location import ChunkLocation
from boba.indexing.metadata import Metadata, MetadataKey
from boba.patterns import StrId

__all__ = [
    "BlockquoteSection",
    "CodeFenceSection",
    "HeadingSection",
    "HorizontalRuleSection",
    "ListSection",
    "ParagraphSection",
    "Section",
    "SectionKeys",
    "SourceId",
    "TableSection",
]

T = TypeVar("T")


class SourceId(StrId):
    """Стабильный canonical id документа-источника (URL, fs-path, doc-key)."""


class SectionKeys:
    """Metadata-ключи для типизированных полей Section, которые попадают в chunk.metadata.

    Используются стандартными подклассами `Section` через `to_chunk_metadata()`.
    Пользовательские подклассы могут добавлять свои `MetadataKey` ClassVar'ы.
    """

    HEADING_LEVEL: ClassVar[MetadataKey[int]] = MetadataKey(
        name="section.heading.level",
        decode=int,
        encode=str,
    )
    HEADING_TEXT: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.heading.text",
        decode=str,
        encode=str,
    )
    CODE_LANGUAGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.code_fence.language",
        decode=str,
        encode=str,
    )
    LIST_ORDERED: ClassVar[MetadataKey[bool]] = MetadataKey(
        name="section.list.ordered",
        decode=lambda s: s == "1",
        encode=lambda v: "1" if v else "0",
    )
    TABLE_HEADER: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.table.header",
        decode=str,
        encode=str,
    )


@dataclass(frozen=True)
class Section(Generic[T]):
    """
    Логический фрагмент документа.

    Базовый класс открытой иерархии.

    Подклассы добавляют типизированные поля
    (`level`, `language`, `header`, `rows`, ...) и переопределяют
    `to_chunk_metadata()` для эмиссии этих полей в `chunk.metadata`.

    Поля:

    - `source_id` — id source-документа (пробрасывается в `Chunk.source_id`).
    - `content` — текст/bytes раздела (для текстовых форматов — `T = str`).
    - `location` — char/byte-offset в исходном raw документе (decoded);
      `original[location.start:location.end] == content`.
    - `anchor` — id для deep-link / chunk_id-стабильности (slug heading'а,
      html-id, и т.п.). `None` если у секции нет естественного якоря.
    - `order` — порядок секции в исходном документе (детерминирует chunk_id).
    - `metadata` — пробрасываемая metadata (от Transport / Reader / merge'ы).
    - `tags` — множество тэгов.
    """

    SECTION_TYPE: ClassVar[str] = "section"
    """Canonical alias типа секции — попадает в `chunk.section_type`. Подклассы переопределяют."""

    source_id: SourceId
    content: T
    # Default `(0, 0)` — для transition (некоторые Reader'ы пока не проставляют);
    # после миграции всех Reader'ов default следует убрать.
    location: ChunkLocation = field(
        default_factory=lambda: ChunkLocation(start=0, end=0),
    )
    anchor: str | None = None
    order: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)

    def to_chunk_metadata(self) -> Metadata:
        """Типизированные поля Section, которые надо положить в `chunk.metadata`.

        Базовый Section не несёт типизированных полей — возвращает `Metadata.empty()`.
        Подклассы переопределяют, чтобы эмитить свои поля через `MetadataKey`'и.
        """
        return Metadata.empty()


@dataclass(frozen=True)
class HeadingSection(Section[str]):
    """
    Раздел с heading-маркером (`# `, `<h1>`, ...).

    Heading-маркер в `content` оригинальный (с разметкой формата —
    `# Title` для markdown, `<h1>Title</h1>` для html);
    `level` и `text` — разобранная типизированная информация
    """

    SECTION_TYPE: ClassVar[str] = "heading"

    level: int = 1
    text: str = ""

    def to_chunk_metadata(self) -> Metadata:
        return (
            Metadata.empty()
            .set(SectionKeys.HEADING_LEVEL, self.level)
            .set(SectionKeys.HEADING_TEXT, self.text)
        )


@dataclass(frozen=True)
class ParagraphSection(Section[str]):
    """Обычный текстовый параграф (inline-разметка остаётся в `content`)."""

    SECTION_TYPE: ClassVar[str] = "paragraph"


@dataclass(frozen=True)
class CodeFenceSection(Section[str]):
    """
    Блок кода в обрамлении (` ```lang ... ``` ` в markdown, `<pre><code>` в html).

    - `language` — language hint (`None` если неизвестен).
    - `code` — содержимое БЕЗ обрамляющих маркеров.
    - `code_line_locations` — char-offset каждой строки тела кода в исходнике.
      Нужен для line-based split при overflow большого блока кода (chunker
      использует, чтобы резать по логическим строкам).
    """

    SECTION_TYPE: ClassVar[str] = "code_fence"

    language: str | None = None
    code: str = ""
    code_line_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.language:
            m = m.set(SectionKeys.CODE_LANGUAGE, self.language)
        return m


@dataclass(frozen=True)
class TableSection(Section[str]):
    """
    Табличный блок (GFM-table, `<table>`, и т.п.).

    - `header` — cells header (распарсенный inline-text каждой ячейки).
    - `rows` — cells каждой data-строки.
    - `header_text` — raw текст header'а вместе с разметкой-разделителем.
      Используется в row-by-row chunker-стратегии: реплицируется в
      `chunk.format_content` перед data-строками, чтобы LLM знала имена
      столбцов даже когда `raw_content` содержит только rows.
    - `header_location` — char-offset header_text в исходном тексте.
    - `row_locations` — char-offset каждой data-строки.
    """

    SECTION_TYPE: ClassVar[str] = "table"

    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    header_text: str = ""
    header_location: ChunkLocation = field(
        default_factory=lambda: ChunkLocation(start=0, end=0),
    )
    row_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.header_text:
            m = m.set(SectionKeys.TABLE_HEADER, self.header_text)
        return m


@dataclass(frozen=True)
class ListSection(Section[str]):
    """
    Список (ordered/unordered).

    - `items` — извлечённый текст каждого top-level item.
    - `item_locations` — char-offset каждого item в исходнике;
      нужен для item-by-item chunking'а при overflow длинного списка.
    """

    SECTION_TYPE: ClassVar[str] = "list"

    ordered: bool = False
    items: tuple[str, ...] = ()
    item_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        return Metadata.empty().set(SectionKeys.LIST_ORDERED, self.ordered)


@dataclass(frozen=True)
class BlockquoteSection(Section[str]):
    """
    Цитата (`> ...` / `<blockquote>`)
    """

    SECTION_TYPE: ClassVar[str] = "blockquote"


@dataclass(frozen=True)
class HorizontalRuleSection(Section[str]):
    """
    Горизонтальная линия / разделитель (`---` / `<hr>`)

    Chunker пропускает эту секцию (не эмитит её в Chunk'и) — это структурный
    разделитель для logical-split, не контентная единица.
    """

    SECTION_TYPE: ClassVar[str] = "horizontal_rule"
