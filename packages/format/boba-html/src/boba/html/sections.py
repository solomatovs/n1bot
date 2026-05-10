"""HTML-специфичные Section-типы.

Format-specific расширения доменной иерархии `boba.indexing.Section`.
Их структура отражает HTML-разметку и не предназначена быть универсальной —
другие форматы (markdown, PDF) имеют свои собственные Section-типы.

Каждый тип несёт per-unit `*_locations` для locality-aware chunking
(table-row split, list-item split, code-line split). Locations — line-precision
(берутся из lxml `.sourceline`); char-offset точно соответствует началу
строки в исходнике.

Структурные шейпы намеренно плоские:
- `HtmlListSection.items` — `tuple[str, ...]` без вложенности (nested-lists
  inлайнятся в текст items'а как есть).
- `HtmlTableSection.rows` — `tuple[tuple[str, ...], ...]` без colspan/rowspan
  (информация теряется; для lossless-таблицы нужен другой тип).

Если потребуется lossless-shape (HTML-таблица с колспаном, multi-level
вложенность списков и т.п.) — добавить отдельные типы рядом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.html.keys import HtmlKeys
from boba.indexing import (
    ChunkLocation,
    FormatBlock,
    FormatPlan,
    Metadata,
    ParagraphSection,
    Section,
)

__all__ = [
    "HtmlBlockquoteSection",
    "HtmlCodeBlockSection",
    "HtmlHorizontalRuleSection",
    "HtmlListSection",
    "HtmlParagraphSection",
    "HtmlTableSection",
]


@dataclass(frozen=True)
class HtmlCodeBlockSection(Section[str]):
    """HTML code-block: `<pre><code class="language-X">...</code></pre>`.

    - `language`            — language hint (из `class="language-X"` или
                              `class="lang-X"`; `None` если не указан).
    - `code`                — текст кода без обрамляющих тегов.
    - `code_line_locations` — char-offset каждой строки тела кода в исходнике
                              (line-precision, для line-based split при
                              overflow).
    """

    SECTION_TYPE: ClassVar[str] = "html.code_block"

    language: str | None = None
    code: str = ""
    code_line_locations: tuple[ChunkLocation, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.language:
            m = m.set(HtmlKeys.CODE_LANGUAGE, self.language)
        return m

    def to_format_plan(self) -> FormatPlan:
        """Fenced markdown code-block. Splitter может резать тело по `\\n`."""
        fence_lang = self.language or ""
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=self.code,
                    raw_content=self.content,
                    location=ChunkLocation(start=0, end=len(self.code)),
                    is_atomic=False,
                ),
            ),
            repeat_header=f"```{fence_lang}\n",
            repeat_footer="\n```",
            block_glue="\n",
        )


@dataclass(frozen=True)
class HtmlTableSection(Section[str]):
    """HTML `<table>` — плоская 2D-проекция.

    Колспан/ровспан/nested-content в ячейках теряются; ячейка → её
    plain-text. Если нужна lossless-таблица — отдельный тип.

    - `header`           — cells `<thead><tr><th>` (plain-text каждой ячейки).
    - `rows`             — cells `<tbody><tr><td>` (plain-text каждой ячейки).
    - `header_text`      — raw текст header'а вместе с разметкой; chunker
                            реплицирует это в `format_content` row-by-row split'а.
    - `header_location`  — char-offset header'а в исходнике (line-precision).
    - `row_locations`    — char-offset каждой data-строки.
    - `row_raw`          — HTML-сериализация каждого `<tr>` (для per-chunk
                            raw_content в row-by-row split). Длина совпадает с
                            `rows`.
    """

    SECTION_TYPE: ClassVar[str] = "html.table"

    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    header_text: str = ""
    header_location: ChunkLocation | None = None
    row_locations: tuple[ChunkLocation, ...] = ()
    row_raw: tuple[str, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        m = Metadata.empty()
        if self.header_text:
            m = m.set(HtmlKeys.TABLE_HEADER, self.header_text)
        return m

    def to_format_plan(self) -> FormatPlan:
        """Markdown-таблица: header реплицируется через `repeat_header`,
        каждый row — atomic `FormatBlock` с собственным `raw_content`.
        """
        md_header = self._render_md_header()
        blocks = tuple(
            FormatBlock(
                format_content="| " + " | ".join(row) + " |",
                raw_content=self.row_raw[i] if i < len(self.row_raw) else self.content,
                location=(
                    self.row_locations[i]
                    if i < len(self.row_locations)
                    else ChunkLocation(start=0, end=0)
                ),
                is_atomic=True,
            )
            for i, row in enumerate(self.rows)
        )
        return FormatPlan(
            blocks=blocks,
            repeat_header=md_header + "\n" if md_header else "",
            block_glue="\n",
        )

    def _render_md_header(self) -> str:
        if not self.header:
            return ""
        head = "| " + " | ".join(self.header) + " |"
        sep = "| " + " | ".join("---" for _ in self.header) + " |"
        return head + "\n" + sep


@dataclass(frozen=True)
class HtmlListSection(Section[str]):
    """HTML `<ul>`/`<ol>` — плоский список top-level items.

    Вложенные списки иlinайнятся в текст соответствующего item'а как plain-text.

    - `ordered`         — `<ol>` (True) или `<ul>` (False).
    - `items`           — plain-text каждого top-level `<li>`.
    - `item_locations`  — char-offset каждого item'а (для item-by-item split).
    - `item_raw`        — HTML-сериализация каждого `<li>` (для per-chunk
                           raw_content). Длина совпадает с `items`.
    """

    SECTION_TYPE: ClassVar[str] = "html.list"

    ordered: bool = False
    items: tuple[str, ...] = ()
    item_locations: tuple[ChunkLocation, ...] = ()
    item_raw: tuple[str, ...] = ()

    def to_chunk_metadata(self) -> Metadata:
        return Metadata.empty().set(HtmlKeys.LIST_ORDERED, self.ordered)

    def to_format_plan(self) -> FormatPlan:
        """Markdown-список: каждый item — atomic блок с собственным raw."""
        blocks = tuple(
            FormatBlock(
                format_content=self._bullet(i) + item,
                raw_content=self.item_raw[i] if i < len(self.item_raw) else self.content,
                location=(
                    self.item_locations[i]
                    if i < len(self.item_locations)
                    else ChunkLocation(start=0, end=0)
                ),
                is_atomic=True,
            )
            for i, item in enumerate(self.items)
        )
        return FormatPlan(blocks=blocks, block_glue="\n")

    def _bullet(self, index: int) -> str:
        return f"{index + 1}. " if self.ordered else "- "


@dataclass(frozen=True)
class HtmlBlockquoteSection(Section[str]):
    """HTML `<blockquote>`.

    - `text` — markdown/plain-text тела (для embedder); если пуст — fallback
               на HTML-сериализацию из `content`.
    """

    SECTION_TYPE: ClassVar[str] = "html.blockquote"

    text: str = ""

    def to_format_plan(self) -> FormatPlan:
        """Markdown blockquote: `> ` префикс на каждой строке."""
        body = self.text or str(self.content)
        quoted = "\n".join("> " + line for line in body.splitlines() if line) or "> "
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=quoted,
                    raw_content=str(self.content),
                    location=ChunkLocation(start=0, end=len(quoted)),
                    is_atomic=False,
                ),
            ),
        )


@dataclass(frozen=True)
class HtmlHorizontalRuleSection(Section[str]):
    """HTML `<hr>`. Структурный маркёр; chunker пропускает (пустой план)."""

    SECTION_TYPE: ClassVar[str] = "html.horizontal_rule"

    def to_format_plan(self) -> FormatPlan:
        return FormatPlan()


@dataclass(frozen=True)
class HtmlParagraphSection(ParagraphSection):
    """HTML `<p>` с уже извлечённым markdown/plain-text телом.

    - `text` — markdown-render (через markdownify) либо plain-text fallback.
                Идёт в embedder. `content` остаётся HTML-сериализацией для
                citation/UI.
    """

    SECTION_TYPE: ClassVar[str] = "html.paragraph"

    text: str = ""

    def to_format_plan(self) -> FormatPlan:
        body = self.text or str(self.content)
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=body,
                    raw_content=str(self.content),
                    location=ChunkLocation(start=0, end=len(body)),
                    is_atomic=False,
                ),
            ),
        )
