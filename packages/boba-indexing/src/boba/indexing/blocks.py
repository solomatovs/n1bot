"""Sealed-class иерархия блоков контента.

`Block` — базовый класс, наследники помечены `@final` и не предполагают
дальнейшего расширения вне этого модуля. Список замкнут: добавление нового
типа блока — изменение здесь + у всех парсеров (markdown / html / ...).

Каждый блок несёт оригинальный `content` (slice исходного текста) и
`location` с char-offset'ами. Структурные поля (level, language, items,
header, rows, ordered) типизированы per-block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, final

from boba.indexing.chunks import ChunkLocation

__all__ = [
    "Block",
    "BlockquoteBlock",
    "CodeFenceBlock",
    "HeadingBlock",
    "HorizontalRuleBlock",
    "HtmlBlock",
    "ListBlock",
    "ParagraphBlock",
    "TableBlock",
]


@dataclass(frozen=True)
class Block:
    """Базовый блок: оригинальный slice + offset в исходнике.

    `content` — slice исходного текста (с сохранением разметки).
    `location.start`/`end` — char-offset'ы в исходнике, причём
    `original[location.start:location.end] == content`.

    Конкретные типы блоков (`HeadingBlock`, `ParagraphBlock`, ...) помечены
    `@final`. Список замкнут — новые типы добавлять только в этом модуле.
    """

    BLOCK_TYPE: ClassVar[str] = "block"

    content: str
    location: ChunkLocation


@final
@dataclass(frozen=True)
class HeadingBlock(Block):
    """Heading произвольного уровня. `level` — 1..6, `text` — без разметки-маркера."""

    BLOCK_TYPE: ClassVar[str] = "heading"

    level: int = 1
    text: str = ""


@final
@dataclass(frozen=True)
class ParagraphBlock(Block):
    """Обычный текстовый параграф (inline-разметка остаётся в content)."""

    BLOCK_TYPE: ClassVar[str] = "paragraph"


@final
@dataclass(frozen=True)
class CodeFenceBlock(Block):
    """Блок кода в обрамлении (markdown ` ``` `, html `<pre><code>`, и т.п.).

    - `language` — language hint (None если неизвестен).
    - `code` — содержимое БЕЗ обрамляющих маркеров.
    - `code_line_locations` — char-offset каждой строки тела кода в исходнике.
      Нужен для line-based split при overflow большого блока кода.
    """

    BLOCK_TYPE: ClassVar[str] = "code_fence"

    language: str | None = None
    code: str = ""
    code_line_locations: tuple[ChunkLocation, ...] = ()


@final
@dataclass(frozen=True)
class TableBlock(Block):
    """Табличный блок (GFM-table в markdown, `<table>` в HTML, и т.п.).

    - `header` — cells header'а (распарсенный inline-text каждой ячейки).
    - `rows` — cells каждой data-строки.
    - `header_text` — raw текст header'а вместе с разметкой-разделителем
      (для markdown — `"| a | b |\\n|---|---|"`). Нужен для row-by-row
      chunking'а: когда чанк содержит только data-строки, header кладётся
      в metadata.
    - `header_location` — char-offset header_text в исходном тексте.
    - `row_locations` — char-offset каждой data-строки.
    """

    BLOCK_TYPE: ClassVar[str] = "table"

    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    header_text: str = ""
    header_location: ChunkLocation = field(
        default_factory=lambda: ChunkLocation(start=0, end=0),
    )
    row_locations: tuple[ChunkLocation, ...] = ()


@final
@dataclass(frozen=True)
class ListBlock(Block):
    """Список (ordered/unordered).

    - `items` — извлечённый текст каждого top-level item.
    - `item_locations` — char-offset каждого item в исходнике;
      нужен для item-by-item chunking'а при overflow длинного списка.
    """

    BLOCK_TYPE: ClassVar[str] = "list"

    ordered: bool = False
    items: tuple[str, ...] = ()
    item_locations: tuple[ChunkLocation, ...] = ()


@final
@dataclass(frozen=True)
class BlockquoteBlock(Block):
    """Цитата. Inline-content остаётся в content как разметка."""

    BLOCK_TYPE: ClassVar[str] = "blockquote"


@final
@dataclass(frozen=True)
class HorizontalRuleBlock(Block):
    """Горизонтальная линия / разделитель (`---` / `<hr>` / etc.)."""

    BLOCK_TYPE: ClassVar[str] = "horizontal_rule"


@final
@dataclass(frozen=True)
class HtmlBlock(Block):
    """Raw HTML внутри документа (e.g. `<div>...</div>` в markdown)."""

    BLOCK_TYPE: ClassVar[str] = "html"
