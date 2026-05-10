"""Markdown AST-парсер: markdown-text → типизированные `Section`-ы.

Использует `markdown-it-py` для AST-парсинга (CommonMark + GFM tables).
Каждый top-level markdown-блок становится конкретным наследником
`boba.indexing.Section[str]` (`HeadingSection`, `ParagraphSection`,
`CodeFenceSection`, `TableSection`, `ListSection`, `BlockquoteSection`,
`HorizontalRuleSection`).

**HTML-блоки внутри markdown** (`<div>...</div>`) представляются как
`ParagraphSection` — сырой HTML кладётся в content как есть, без отдельного
типа (он markdown-специфичен и не нужен в доменной иерархии).

**Inline-форматирование** (bold/italic/links/inline-code) НЕ разворачивается
в отдельные секции — остаётся внутри `content` как markdown-syntax.

Зависимость: `markdown-it-py` (опциональная). Установка:
`pip install markdown-it-py` или `pip install boba-markdown[structural]`.

**Контракт offset-tracking**: для любой Section `s`,
`original_text[s.location.start:s.location.end] == s.content`.

Парсер также `source_id` и `base_metadata` для каждой эмитируемой Section,
чтобы `Reader.convert(raw)` мог напрямую `yield from parser.parse(...)`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from boba.indexing import (
    ChunkLocation,
    HeadingSection,
    Metadata,
    ParagraphSection,
    Section,
    SectionKeys,
    SourceId,
)
from boba.markdown.sections import (
    MarkdownBlockquoteSection,
    MarkdownCodeFenceSection,
    MarkdownHorizontalRuleSection,
    MarkdownListSection,
    MarkdownTableSection,
)


def _with_location(
    base: Metadata, start: int, end: int, anchor: str | None = None,
) -> Metadata:
    """Кладёт координаты секции (и опционально anchor) в metadata."""
    md = base.set(SectionKeys.LOCATION_START, start).set(
        SectionKeys.LOCATION_END, end
    )
    if anchor is not None:
        md = md.set(SectionKeys.ANCHOR, anchor)
    return md

__all__ = ["MarkdownSectionParser", "slugify"]


_NONALNUM = re.compile(r"[^\w\-]+", flags=re.UNICODE)


def slugify(text: str) -> str | None:
    """`Foo Bar! 123` → `foo-bar-123`. Возвращает `None` для пустого результата."""
    s = text.strip().lower().replace(" ", "-")
    s = _NONALNUM.sub("", s)
    s = s.strip("-")
    return s or None


class MarkdownSectionParser:
    """Парсит markdown в поток `Section[str]`-ов с offset-tracking.

    Использует `markdown-it-py` для block-level AST. Каждый top-level
    AST-токен → соответствующий `Section`-наследник.

    **Зависимость**: `markdown-it-py` (lazy-import в `__init__`).
    """

    def __init__(self) -> None:
        try:
            from markdown_it import MarkdownIt
        except ImportError as e:
            raise ImportError(
                "MarkdownSectionParser requires `markdown-it-py`. "
                "Install: `pip install markdown-it-py` or "
                "`pip install boba-markdown[structural]`."
            ) from e
        self._md = MarkdownIt("commonmark", {"html": True}).enable("table")

    def parse(
        self,
        text: str,
        *,
        source_id: SourceId,
        base_metadata: Metadata,
    ) -> Iterable[Section[str]]:
        """Парсит `text` в поток `Section`-ов.

        `source_id` и `base_metadata` копируются в каждую эмитированную секцию.
        `order` присваивается монотонно (0, 1, 2, ...) — по порядку появления
        секций в документе; нужен для детерминизма chunk_id.
        """
        if not text:
            return
        tokens = self._md.parse(text)
        line_offsets = self._compute_line_offsets(text)
        order = 0
        i = 0
        while i < len(tokens):
            section, advance = self._parse_top_level(
                tokens, i, text, line_offsets,
                source_id=source_id,
                base_metadata=base_metadata,
                order=order,
            )
            if section is not None:
                yield section
                order += 1
            i += advance

    @staticmethod
    def _compute_line_offsets(text: str) -> list[int]:
        offsets = [0]
        pos = 0
        for line in text.split("\n"):
            pos += len(line) + 1
            offsets.append(pos)
        return offsets

    @staticmethod
    def _slice_for_map(
        text: str,
        line_offsets: list[int],
        map_range: list[int],
    ) -> tuple[int, int, str]:
        line_start, line_end = map_range[0], map_range[1]
        char_start = line_offsets[line_start]
        char_end = (
            line_offsets[line_end] if line_end < len(line_offsets) else len(text)
        )
        char_end = min(char_end, len(text))
        while char_end > char_start and text[char_end - 1] == "\n":
            char_end -= 1
        return char_start, char_end, text[char_start:char_end]

    def _parse_top_level(
        self,
        tokens: list,
        i: int,
        text: str,
        line_offsets: list[int],
        *,
        source_id: SourceId,
        base_metadata: Metadata,
        order: int,
    ) -> tuple[Section[str] | None, int]:
        """Парсит один top-level block начиная с tokens[i]; возвращает (section, advance)."""
        tok = tokens[i]
        kwargs = {
            "source_id": source_id,
            "base_metadata": base_metadata,
            "order": order,
        }
        if tok.type == "heading_open":
            return self._parse_heading(tokens, i, text, line_offsets, **kwargs)
        if tok.type == "paragraph_open":
            return self._parse_paragraph(tokens, i, text, line_offsets, **kwargs)
        if tok.type == "fence":
            return self._parse_fence(tok, text, line_offsets, **kwargs)
        if tok.type == "table_open":
            return self._parse_table(tokens, i, text, line_offsets, **kwargs)
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            return self._parse_list(
                tokens, i, text, line_offsets,
                ordered=tok.type == "ordered_list_open",
                **kwargs,
            )
        if tok.type == "blockquote_open":
            return self._parse_blockquote(tokens, i, text, line_offsets, **kwargs)
        if tok.type == "hr":
            return self._parse_hr(tok, text, line_offsets, **kwargs)
        if tok.type == "html_block":
            # Raw HTML внутри markdown → представляем как ParagraphSection
            # (HTML-специфика не в доменной иерархии Section).
            return self._parse_html_block(tok, text, line_offsets, **kwargs)
        return None, 1

    @classmethod
    def _parse_heading(
        cls, tokens, i, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "heading_close") + 1
        level = int(tok.tag[1:])
        heading_text = tokens[i + 1].content if tokens[i + 1].type == "inline" else ""
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        section = HeadingSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(
                base_metadata, start, end, anchor=slugify(heading_text),
            ),
            level=level,
            text=heading_text,
        )
        advance = cls._skip_until(tokens, i + 1, "heading_close") + 1
        return section, advance

    @classmethod
    def _parse_paragraph(
        cls, tokens, i, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "paragraph_close") + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        section = ParagraphSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
        )
        advance = cls._skip_until(tokens, i + 1, "paragraph_close") + 1
        return section, advance

    @classmethod
    def _parse_fence(
        cls, tok, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        info = tok.info.strip()
        body_first_line = tok.map[0] + 1
        body_last_line = tok.map[1] - 1
        line_locs: list[ChunkLocation] = []
        for line_no in range(body_first_line, body_last_line):
            ls, le, _ = cls._slice_for_map(text, line_offsets, [line_no, line_no + 1])
            line_locs.append(ChunkLocation(start=ls, end=le))
        section = MarkdownCodeFenceSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
            language=info or None,
            code=tok.content,
            code_line_locations=tuple(line_locs),
        )
        return section, 1

    @classmethod
    def _parse_table(  # noqa: C901, PLR0912
        cls, tokens, i, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "table_close") + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        header: list[str] = []
        rows: list[list[str]] = []
        row_locs: list[ChunkLocation] = []
        cur_row: list[str] = []
        in_header = False
        in_body = False
        thead_start_line: int | None = None
        tbody_start_line: int | None = None
        cur_row_map: list[int] | None = None
        j = i + 1
        while j < len(tokens) and tokens[j].type != "table_close":
            t = tokens[j]
            if t.type == "thead_open":
                in_header = True
                if t.map:
                    thead_start_line = t.map[0]
            elif t.type == "thead_close":
                in_header = False
            elif t.type == "tbody_open":
                in_body = True
                if t.map:
                    tbody_start_line = t.map[0]
            elif t.type == "tbody_close":
                in_body = False
            elif t.type == "tr_open":
                cur_row_map = list(t.map) if t.map else None
            elif t.type == "inline":
                cur_row.append(t.content)
            elif t.type == "tr_close":
                if in_header:
                    header = cur_row
                elif in_body:
                    rows.append(cur_row)
                    if cur_row_map is not None:
                        rs, re_, _ = cls._slice_for_map(text, line_offsets, cur_row_map)
                        row_locs.append(ChunkLocation(start=rs, end=re_))
                cur_row = []
                cur_row_map = None
            j += 1
        if thead_start_line is not None and tbody_start_line is not None:
            hs, he, htext = cls._slice_for_map(
                text, line_offsets, [thead_start_line, tbody_start_line]
            )
        else:
            hs, he, htext = start, start, ""
        section = MarkdownTableSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
            header=tuple(header),
            rows=tuple(tuple(r) for r in rows),
            header_text=htext,
            header_location=ChunkLocation(start=hs, end=he),
            row_locations=tuple(row_locs),
        )
        return section, j - i + 1

    @classmethod
    def _parse_list(
        cls, tokens, i, text, line_offsets,
        *, ordered, source_id, base_metadata, order,
    ):
        tok = tokens[i]
        close_type = "ordered_list_close" if ordered else "bullet_list_close"
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, close_type) + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        items: list[str] = []
        item_locs: list[ChunkLocation] = []
        depth = 1
        cur_item_parts: list[str] = []
        cur_item_map: list[int] | None = None
        in_top_level_item = False
        j = i + 1
        while j < len(tokens):
            t = tokens[j]
            if t.type in ("bullet_list_open", "ordered_list_open"):
                depth += 1
            elif t.type in ("bullet_list_close", "ordered_list_close"):
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and t.type == "list_item_open":
                in_top_level_item = True
                cur_item_parts = []
                cur_item_map = list(t.map) if t.map else None
            elif depth == 1 and t.type == "list_item_close":
                in_top_level_item = False
                items.append("\n".join(p for p in cur_item_parts if p))
                if cur_item_map is not None:
                    is_, ie, _ = cls._slice_for_map(text, line_offsets, cur_item_map)
                    item_locs.append(ChunkLocation(start=is_, end=ie))
                cur_item_map = None
            elif in_top_level_item and t.type == "inline":
                cur_item_parts.append(t.content)
            j += 1
        section = MarkdownListSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
            ordered=ordered,
            items=tuple(items),
            item_locations=tuple(item_locs),
        )
        return section, j - i + 1

    @classmethod
    def _parse_blockquote(
        cls, tokens, i, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "blockquote_close") + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        section = MarkdownBlockquoteSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
        )
        depth = 1
        j = i + 1
        while j < len(tokens) and depth > 0:
            if tokens[j].type == "blockquote_open":
                depth += 1
            elif tokens[j].type == "blockquote_close":
                depth -= 1
            j += 1
        return section, j - i

    @classmethod
    def _parse_hr(
        cls, tok, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        section = MarkdownHorizontalRuleSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
        )
        return section, 1

    @classmethod
    def _parse_html_block(
        cls, tok, text, line_offsets,
        *, source_id, base_metadata, order,
    ):
        """Raw HTML внутри markdown → ParagraphSection (HTML-фрагмент как plain content)."""
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        section = ParagraphSection(
            source_id=source_id,
            content=content,
            order=order,
            metadata=_with_location(base_metadata, start, end),
        )
        return section, 1

    @staticmethod
    def _skip_until(tokens: list, start: int, close_type: str) -> int:
        j = start
        while j < len(tokens) and tokens[j].type != close_type:
            j += 1
        return j - start + 1
