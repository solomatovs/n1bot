"""Markdown block-level AST через `markdown-it-py`.

Парсит markdown-документ в типизированные блоки из `boba.indexing` (heading,
paragraph, code-fence, table, list, blockquote, hr, html) с offset-tracking.

Inline-форматирование (bold/italic/links/inline-code) НЕ разворачивается
в отдельные блоки — остаётся внутри `content` как markdown-syntax.

Зависимость: `markdown-it-py` (опциональная). Установка:
`pip install markdown-it-py` или `pip install boba-markdown[markdown_structural]`.
"""

from __future__ import annotations

from boba.indexing import (
    Block,
    BlockquoteBlock,
    CodeFenceBlock,
    HeadingBlock,
    HorizontalRuleBlock,
    HtmlBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
)
from boba.indexing import ChunkLocation

__all__ = ["MarkdownBlockParser"]


class MarkdownBlockParser:
    """
    Парсит markdown в поток типизированных `boba.indexing.Block`-наследников.

    Использует `markdown-it-py` для AST-парсинга (CommonMark + GFM tables).
    Каждый top-level markdown-блок становится конкретным `Block`-наследником
    (`HeadingBlock`, `CodeFenceBlock`, `TableBlock`, ...); inline-форматирование
    остаётся внутри `content` как markdown-syntax.

    **Схема** (трансформация):
    ```python
    str (markdown)   ──parser.parse──→  list[Block]
        │                                  │
        │                                  ├─ HeadingBlock (level, text, content, location)
        │                                  ├─ ParagraphBlock (content, location)
        │                                  ├─ CodeFenceBlock (language, code, code_line_locations, content, location)
        │                                  ├─ TableBlock (header, rows, header_text, row_locations, content, location)
        │                                  ├─ ListBlock (ordered, items, item_locations, content, location)
        │                                  ├─ BlockquoteBlock (content, location)
        │                                  ├─ HorizontalRuleBlock (content, location)
        │                                  └─ HtmlBlock (content, location)
        │
        └─ для каждого блока: original[location.start:location.end] == content
    ```

    **Контракт offset-tracking**: для любого блока `b`,
    `original_text[b.location.start:b.location.end] == b.content` —
    блок несёт **точный slice** исходного текста, без репликации или
    нормализации. Структурную интерпретацию (cells таблицы, items списка)
    парсер кладёт в типизированные поля рядом.

    **Пример**:
    ```python
    parser = MarkdownBlockParser()

    md = '''# Title

    intro paragraph.

    ```python
    print("hi")
    ```

    | a | b |
    |---|---|
    | 1 | 2 |

    - item one
    - item two

    > quote

    ---
    '''

    blocks = parser.parse(md)
    # → [
    #     HeadingBlock(level=1, text="Title", content="# Title", location=...),
    #     ParagraphBlock(content="intro paragraph.", location=...),
    #     CodeFenceBlock(language="python", code='print("hi")\\n',
    #                    content='```python\\nprint("hi")\\n```', location=...),
    #     TableBlock(header=("a", "b"), rows=(("1", "2"),),
    #                content="| a | b |\\n|---|---|\\n| 1 | 2 |", location=...),
    #     ListBlock(ordered=False, items=("item one", "item two"),
    #               content="- item one\\n- item two", location=...),
    #     BlockquoteBlock(content="> quote", location=...),
    #     HorizontalRuleBlock(content="---", location=...),
    # ]
    ```
    """  # noqa: E501

    def __init__(self) -> None:
        try:
            from markdown_it import MarkdownIt
        except ImportError as e:
            raise ImportError(
                "MarkdownBlockParser requires `markdown-it-py`. "
                "Install: `pip install markdown-it-py` or "
                "`pip install boba-markdown[markdown_structural]`."
            ) from e
        self._md = MarkdownIt("commonmark", {"html": True}).enable("table")

    def parse(self, text: str) -> list[Block]:
        """Парсит `text` в поток типизированных блоков."""
        if not text:
            return []
        tokens = self._md.parse(text)
        line_offsets = self._compute_line_offsets(text)
        result: list[Block] = []
        i = 0
        while i < len(tokens):
            block, advance = self._parse_top_level(tokens, i, text, line_offsets)
            if block is not None:
                result.append(block)
            i += advance
        return result

    @staticmethod
    def _compute_line_offsets(text: str) -> list[int]:
        """Возвращает char-offset начала каждой строки (0-based)."""
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
        """`token.map` → (char_start, char_end, content_slice)."""
        line_start, line_end = map_range[0], map_range[1]
        char_start = line_offsets[line_start]
        char_end = (
            line_offsets[line_end] if line_end < len(line_offsets) else len(text)
        )
        # `line_offsets` хранит позицию ПОСЛЕ \n; для последней строки без
        # реального \n позиция уходит за конец → clamp к len(text).
        char_end = min(char_end, len(text))
        # Trim trailing newline для аккуратности (но без trim самого content).
        while char_end > char_start and text[char_end - 1] == "\n":
            char_end -= 1
        return char_start, char_end, text[char_start:char_end]

    def _parse_top_level(
        self, tokens: list, i: int, text: str, line_offsets: list[int]
    ) -> tuple[Block | None, int]:
        """Парсит один top-level block начиная с tokens[i]; возвращает (block, advance)."""
        tok = tokens[i]
        if tok.type == "heading_open":
            return self._parse_heading(tokens, i, text, line_offsets)
        if tok.type == "paragraph_open":
            return self._parse_paragraph(tokens, i, text, line_offsets)
        if tok.type == "fence":
            return self._parse_fence(tok, text, line_offsets)
        if tok.type == "table_open":
            return self._parse_table(tokens, i, text, line_offsets)
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            return self._parse_list(
                tokens, i, text, line_offsets, ordered=tok.type == "ordered_list_open"
            )
        if tok.type == "blockquote_open":
            return self._parse_blockquote(tokens, i, text, line_offsets)
        if tok.type == "hr":
            return self._parse_hr(tok, text, line_offsets)
        if tok.type == "html_block":
            return self._parse_html(tok, text, line_offsets)
        return None, 1

    @classmethod
    def _parse_heading(
        cls, tokens: list, i: int, text: str, line_offsets: list[int]
    ) -> tuple[HeadingBlock | None, int]:
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "heading_close") + 1
        level = int(tok.tag[1:])
        # Следующий — inline token c heading-текстом.
        heading_text = tokens[i + 1].content if tokens[i + 1].type == "inline" else ""
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        block = HeadingBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
            level=level,
            text=heading_text,
        )
        advance = cls._skip_until(tokens, i + 1, "heading_close") + 1
        return block, advance

    @classmethod
    def _parse_paragraph(
        cls, tokens: list, i: int, text: str, line_offsets: list[int]
    ) -> tuple[ParagraphBlock | None, int]:
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "paragraph_close") + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        block = ParagraphBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
        )
        advance = cls._skip_until(tokens, i + 1, "paragraph_close") + 1
        return block, advance

    @classmethod
    def _parse_fence(
        cls, tok, text: str, line_offsets: list[int]
    ) -> tuple[CodeFenceBlock | None, int]:
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        info = tok.info.strip()
        # Body code lives between opening and closing fence:
        # body lines = [tok.map[0] + 1 .. tok.map[1] - 1)  (exclusive of closing fence).
        body_first_line = tok.map[0] + 1
        body_last_line = tok.map[1] - 1  # exclusive
        line_locs: list[ChunkLocation] = []
        for line_no in range(body_first_line, body_last_line):
            ls, le, _ = cls._slice_for_map(
                text, line_offsets, [line_no, line_no + 1]
            )
            line_locs.append(ChunkLocation(start=ls, end=le))
        block = CodeFenceBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
            language=info or None,
            code=tok.content,
            code_line_locations=tuple(line_locs),
        )
        return block, 1

    @classmethod
    def _parse_table(  # noqa: C901, PLR0912
        cls, tokens: list, i: int, text: str, line_offsets: list[int]
    ) -> tuple[TableBlock | None, int]:
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
        # Header_text покрывает header + separator-строку. Берём от thead.map[0]
        # до tbody.map[0] (если tbody есть) — это даёт обе строки GFM-header'а.
        thead_start_line: int | None = None
        tbody_start_line: int | None = None
        cur_row_map: list[int] | None = None  # map текущей tr (для row_locations)
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
        # Header_text + separator: lines [thead_start_line .. tbody_start_line).
        if thead_start_line is not None and tbody_start_line is not None:
            hs, he, htext = cls._slice_for_map(
                text, line_offsets, [thead_start_line, tbody_start_line]
            )
        else:
            hs, he, htext = start, start, ""
        block = TableBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
            header=tuple(header),
            rows=tuple(tuple(r) for r in rows),
            header_text=htext,
            header_location=ChunkLocation(start=hs, end=he),
            row_locations=tuple(row_locs),
        )
        return block, j - i + 1

    @classmethod
    def _parse_list(
        cls,
        tokens: list,
        i: int,
        text: str,
        line_offsets: list[int],
        *,
        ordered: bool,
    ) -> tuple[ListBlock | None, int]:
        tok = tokens[i]
        close_type = "ordered_list_close" if ordered else "bullet_list_close"
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, close_type) + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        items: list[str] = []
        item_locs: list[ChunkLocation] = []
        depth = 1  # текущий уровень вложенности списка
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
        block = ListBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
            ordered=ordered,
            items=tuple(items),
            item_locations=tuple(item_locs),
        )
        return block, j - i + 1

    @classmethod
    def _parse_blockquote(
        cls, tokens: list, i: int, text: str, line_offsets: list[int]
    ) -> tuple[BlockquoteBlock | None, int]:
        tok = tokens[i]
        if not tok.map:
            return None, cls._skip_until(tokens, i + 1, "blockquote_close") + 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        block = BlockquoteBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
        )
        # Skip nested blockquote_open/close pairs.
        depth = 1
        j = i + 1
        while j < len(tokens) and depth > 0:
            if tokens[j].type == "blockquote_open":
                depth += 1
            elif tokens[j].type == "blockquote_close":
                depth -= 1
            j += 1
        return block, j - i

    @classmethod
    def _parse_hr(
        cls, tok, text: str, line_offsets: list[int]
    ) -> tuple[HorizontalRuleBlock | None, int]:
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        return HorizontalRuleBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
        ), 1

    @classmethod
    def _parse_html(
        cls, tok, text: str, line_offsets: list[int]
    ) -> tuple[HtmlBlock | None, int]:
        if not tok.map:
            return None, 1
        start, end, content = cls._slice_for_map(text, line_offsets, tok.map)
        return HtmlBlock(
            content=content,
            location=ChunkLocation(start=start, end=end),
        ), 1

    @staticmethod
    def _skip_until(tokens: list, start: int, close_type: str) -> int:
        """Возвращает количество токенов от start до встречи close_type включительно."""
        j = start
        while j < len(tokens) and tokens[j].type != close_type:
            j += 1
        return j - start + 1
