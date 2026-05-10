"""HTML AST-парсер: HTML-text → типизированные `Section`.

Использует `lxml` для AST-парсинга; offset-tracking — line-precision через
`Element.sourceline` плюс предупреждённые line-offsets исходного текста.

Каждый top-level structural-блок становится конкретным наследником
`boba.indexing.Section[str]`:

- `<h1>..<h6>`        → `HeadingSection`
- `<p>`               → `ParagraphSection`
- `<ul>`/`<ol>`       → `HtmlListSection`
- `<table>`           → `HtmlTableSection`
- `<pre><code>`       → `HtmlCodeBlockSection`
- `<blockquote>`      → `HtmlBlockquoteSection`
- `<hr>`              → `HtmlHorizontalRuleSection`

**Wrapper-теги** (`<div>`, `<section>`, `<article>`, `<main>`, `<aside>`,
`<header>`, `<footer>`, `<nav>`) рассматриваются как прозрачные — парсер
рекурсивно обходит их детей.

**Noise-теги** (`<script>`, `<style>`) удаляются до обхода.

**Контракт offset-tracking** (line-precision): для любой Section `s`
парсер кладёт в её `metadata` `SectionKeys.LOCATION_START/END`,
указывающие на строки исходника где находится соответствующий блок.
Точность — границы строк (lxml не даёт column-offset).

**Section.content** — HTML-сериализация элемента (через `etree.tostring`),
а не прямой срез `text[start:end]`. Причина: при line-precision tracking'е
несколько блоков на одной строке исходника имеют одинаковый range, и
прямой срез захватил бы sibling'ов. Сериализация AST даёт чистый
per-element HTML после noise-stripping'а. Slice-инвариант
`text[loc.start:loc.end] == content` для HTML НЕ выполняется — `LOCATION_*`
указывает «где это в source», `content` несёт «что это».

Inline-форматирование (`<strong>`/`<em>`/`<a>`/`<code>`-внутри-параграфа)
НЕ разворачивается в отдельные секции — остаётся в `content` как HTML-syntax.

Зависимость: `lxml` (обязательная — пакет без неё не работает).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import lxml.etree as lxml_etree
from lxml import html as lxml_html
from lxml.etree import _Element

from boba.html.sections import (
    HtmlBlockquoteSection,
    HtmlCodeBlockSection,
    HtmlHorizontalRuleSection,
    HtmlListSection,
    HtmlParagraphSection,
    HtmlTableSection,
)
from boba.indexing import (
    ChunkLocation,
    HeadingSection,
    Metadata,
    Section,
    SectionKeys,
    SourceId,
)

try:
    import markdownify as _markdownify
except ImportError:  # optional dep — see boba-html[markdown]
    _markdownify = None  # type: ignore[assignment]

__all__ = ["HtmlSectionParser", "slugify"]


_NONALNUM = re.compile(r"[^\w\-]+", flags=re.UNICODE)
_NOISE_TAGS = ("script", "style")
_HEADING_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))
_STRUCTURAL_TAGS = frozenset(
    (*_HEADING_TAGS, "p", "ul", "ol", "table", "pre", "blockquote", "hr"),
)
_WRAPPER_TAGS = frozenset(
    (
        "body",
        "div",
        "section",
        "article",
        "main",
        "aside",
        "header",
        "footer",
        "nav",
    )
)


# Возвращаемое значение всех _parse_*-helper'ов.
_ParseResult = Section[str] | None


def slugify(text: str) -> str | None:
    """`Foo Bar! 123` → `foo-bar-123`. Возвращает `None` для пустого результата."""
    s = text.strip().lower().replace(" ", "-")
    s = _NONALNUM.sub("", s)
    s = s.strip("-")
    return s or None


def _compute_line_offsets(text: str) -> list[int]:
    """Префиксные char-offset'ы каждой строки `text` (включая виртуальную
    строку после последней — для half-open-range).
    """
    offsets = [0]
    pos = 0
    for line in text.split("\n"):
        pos += len(line) + 1
        offsets.append(pos)
    return offsets


def _with_location(
    base: Metadata,
    start: int,
    end: int,
    anchor: str | None = None,
) -> Metadata:
    """Кладёт координаты секции (и опционально anchor) в metadata."""
    md = base.set(SectionKeys.LOCATION_START, start).set(
        SectionKeys.LOCATION_END,
        end,
    )
    if anchor is not None:
        md = md.set(SectionKeys.ANCHOR, anchor)
    return md


def _tag_name(el: _Element) -> str | None:
    """Lower-case-имя элемента, или `None` если это comment / PI / иное."""
    return el.tag.lower() if isinstance(el.tag, str) else None


def _text_content(el: _Element) -> str:
    """Plain-text всего поддерева, с нормализацией whitespace."""
    raw = el.text_content() if hasattr(el, "text_content") else "".join(el.itertext())
    return " ".join(raw.split())


def _attr_class(el: _Element) -> str:
    """`class`-атрибут, нормализованный в строку."""
    cls = el.get("class")
    return cls if isinstance(cls, str) else ""


def _serialize(el: _Element) -> str:
    """HTML-сериализация элемента (с тегом и children).

    После noise-stripping'а (drop_tree script/style) сериализация даёт
    чистый HTML без вырезанных тегов; в отличие от прямого среза
    `text[start:end]` который при line-precision tracking может захватить
    sibling-элементы лежащие на той же строке.
    """
    out = lxml_etree.tostring(el, method="html", encoding="unicode")
    return out if isinstance(out, str) else str(out)


def _inline_markdown(el: _Element) -> str:
    """Markdown-версия inline-контента элемента: bold/italic/links/code.

    Если `markdownify` не установлен (опц. dep `boba-html[markdown]`) —
    fallback на plain-text. inline-контент: содержимое одного блочного тега
    (`<p>`, `<blockquote>`) без обрамляющего открывающего/закрывающего тега.
    """
    if _markdownify is None:
        return _text_content(el)
    inner_html = _serialize_inner(el)
    md = _markdownify.markdownify(inner_html, heading_style="ATX")
    return md.strip()


def _serialize_inner(el: _Element) -> str:
    """Inner HTML элемента (без обрамляющего тега самого `el`)."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el.iterchildren():
        parts.append(
            lxml_etree.tostring(child, method="html", encoding="unicode", with_tail=True),
        )
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class _ParseContext:
    """Session-invariant вход парсера."""

    text: str
    line_offsets: list[int]
    source_id: SourceId
    base_metadata: Metadata

    def line_to_offset(self, line: int) -> int:
        """1-based line number → char-offset начала строки."""
        idx = line - 1
        if idx < 0:
            return 0
        if idx >= len(self.line_offsets):
            return len(self.text)
        return self.line_offsets[idx]

    def line_range(
        self,
        start_line: int,
        end_line: int | None,
    ) -> tuple[int, int]:
        """1-based `[start_line, end_line)` → `(start_offset, end_offset)`.

        Если `end_line` is None — диапазон до конца текста. Trailing
        whitespace/newline отбрасывается.
        """
        start = self.line_to_offset(start_line)
        if end_line is None:
            end = len(self.text)
        else:
            end = self.line_to_offset(end_line)
        end = min(end, len(self.text))
        while end > start and self.text[end - 1] in (" ", "\n", "\r", "\t"):
            end -= 1
        return start, end


class HtmlSectionParser:
    """Парсит HTML в поток `Section[str]` с line-precision offset-tracking.

    Использует `lxml` для AST. Каждый top-level structural-блок →
    соответствующий `Section`-наследник; wrapper-теги (`<div>`/`<section>`/...)
    транзитивно обходятся.
    """

    def parse(
        self,
        text: str,
        *,
        source_id: SourceId,
        base_metadata: Metadata,
    ) -> Iterable[Section[str]]:
        """Парсит `text` в поток `Section`-ов.

        `source_id` и `base_metadata` копируются в каждую эмитированную секцию.
        `order` присваивается монотонно по document-order.
        """
        if not text.strip():
            return
        try:
            root = lxml_html.fromstring(text.encode("utf-8"))
        except (lxml_html.etree.ParserError, ValueError):  # type: ignore[attr-defined]
            return
        for noise in root.iter(*_NOISE_TAGS):
            noise.drop_tree()
        body = self._find_body(root)
        if body is None:
            return
        ctx = _ParseContext(
            text=text,
            line_offsets=_compute_line_offsets(text),
            source_id=source_id,
            base_metadata=base_metadata,
        )
        blocks = list(self._walk_structural(body))
        for i, el in enumerate(blocks):
            next_line = blocks[i + 1].sourceline if i + 1 < len(blocks) else None
            section = self._dispatch(ctx, el, order=i, next_line=next_line)
            if section is not None:
                yield section

    @staticmethod
    def _find_body(root: _Element) -> _Element | None:
        """Найти `<body>`; fallback — root, если он сам структурный."""
        if _tag_name(root) == "body":
            return root
        body = root.find(".//body")
        if body is not None:
            return body
        # Допускаем root без body (фрагмент HTML без обёртки).
        return root

    @classmethod
    def _walk_structural(cls, el: _Element) -> Iterator[_Element]:
        """Top-level structural блоки в document-order.

        Wrapper-теги транзитивно раскрываются; не-structural / не-wrapper
        дети пропускаются.
        """
        for child in el.iterchildren():
            tag = _tag_name(child)
            if tag is None:
                continue
            if tag in _STRUCTURAL_TAGS:
                yield child
            elif tag in _WRAPPER_TAGS:
                yield from cls._walk_structural(child)
            # else: skip (inline-tag без обёрткой структуры)

    def _dispatch(  # noqa: PLR0911
        self,
        ctx: _ParseContext,
        el: _Element,
        order: int,
        next_line: int | None,
    ) -> _ParseResult:
        tag = _tag_name(el)
        if tag is None:
            return None
        sourceline = el.sourceline or 1
        start, end = ctx.line_range(sourceline, next_line)
        if tag in _HEADING_TAGS:
            return self._parse_heading(ctx, el, order, start, end)
        if tag == "p":
            return self._parse_paragraph(ctx, el, order, start, end)
        if tag in ("ul", "ol"):
            return self._parse_list(
                ctx,
                el,
                order,
                start,
                end,
                ordered=(tag == "ol"),
            )
        if tag == "table":
            return self._parse_table(ctx, el, order, start, end)
        if tag == "pre":
            return self._parse_code_block(ctx, el, order, start, end)
        if tag == "blockquote":
            return self._parse_blockquote(ctx, el, order, start, end)
        if tag == "hr":
            return self._parse_hr(ctx, el, order, start, end)
        return None

    @staticmethod
    def _parse_heading(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        tag = _tag_name(el)
        if tag is None:
            return None
        level = int(tag[1:])
        heading_text = _text_content(el)
        anchor = el.get("id") or slugify(heading_text)
        content = _serialize(el)
        return HeadingSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end, anchor=anchor),
            level=level,
            text=heading_text,
        )

    @staticmethod
    def _parse_paragraph(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        content = _serialize(el)
        text = _inline_markdown(el)
        return HtmlParagraphSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
            text=text,
        )

    @staticmethod
    def _parse_list(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
        *,
        ordered: bool,
    ) -> _ParseResult:
        # Top-level <li> — прямые дети <ul>/<ol>.
        items: list[str] = []
        item_locs: list[ChunkLocation] = []
        item_raw: list[str] = []
        li_children = [c for c in el.iterchildren() if _tag_name(c) == "li"]
        for i, li in enumerate(li_children):
            items.append(_text_content(li))
            item_raw.append(_serialize(li))
            li_start_line = li.sourceline or 1
            li_end_line = (
                li_children[i + 1].sourceline if i + 1 < len(li_children) else None
            )
            ls, le = ctx.line_range(li_start_line, li_end_line)
            item_locs.append(ChunkLocation(start=ls, end=le))
        content = _serialize(el)
        return HtmlListSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
            ordered=ordered,
            items=tuple(items),
            item_locations=tuple(item_locs),
            item_raw=tuple(item_raw),
        )

    @staticmethod
    def _parse_table(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        header: tuple[str, ...] = ()
        rows: list[list[str]] = []
        row_locs: list[ChunkLocation] = []
        header_text = ""
        header_location: ChunkLocation | None = None

        thead = el.find("thead")
        tbody = el.find("tbody")
        # Header: первый <tr> внутри <thead>, иначе первый <tr> в таблице
        # с <th>-ячейками.
        header_tr: _Element | None = None
        if thead is not None:
            first_tr = thead.find(".//tr")
            if first_tr is not None:
                header_tr = first_tr
        if header_tr is None:
            for tr in el.iter("tr"):
                if any(_tag_name(c) == "th" for c in tr.iterchildren()):
                    header_tr = tr
                    break
        if header_tr is not None:
            header = tuple(
                _text_content(c)
                for c in header_tr.iterchildren()
                if _tag_name(c) in ("th", "td")
            )
            hl = header_tr.sourceline or 1
            hs, he = ctx.line_range(hl, hl + 1)
            header_text = ctx.text[hs:he]
            header_location = ChunkLocation(start=hs, end=he)

        # Body rows: <tr> в <tbody> (или просто все <tr> кроме header).
        body_trs: list[_Element]
        if tbody is not None:
            body_trs = [tr for tr in tbody.iter("tr") if tr is not header_tr]
        else:
            body_trs = [tr for tr in el.iter("tr") if tr is not header_tr]
        row_raw: list[str] = []
        for i, tr in enumerate(body_trs):
            row = [
                _text_content(c)
                for c in tr.iterchildren()
                if _tag_name(c) in ("td", "th")
            ]
            rows.append(row)
            row_raw.append(_serialize(tr))
            tr_start_line = tr.sourceline or 1
            tr_end_line = body_trs[i + 1].sourceline if i + 1 < len(body_trs) else None
            rs, re_ = ctx.line_range(tr_start_line, tr_end_line)
            row_locs.append(ChunkLocation(start=rs, end=re_))

        content = _serialize(el)
        return HtmlTableSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
            header=header,
            rows=tuple(tuple(r) for r in rows),
            header_text=header_text,
            header_location=header_location,
            row_locations=tuple(row_locs),
            row_raw=tuple(row_raw),
        )

    @staticmethod
    def _parse_code_block(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        # Body — внутренний <code> если есть, иначе сам <pre>.
        code_el = el.find("code")
        body_el = code_el if code_el is not None else el
        code = (
            body_el.text_content()
            if hasattr(body_el, "text_content")
            else ("".join(body_el.itertext()))
        )

        # Language hint: class="language-X" / "lang-X" / "highlight-X" на <code>
        # или на <pre>.
        language: str | None = None
        for source_el in (code_el, el):
            if source_el is None:
                continue
            for token in _attr_class(source_el).split():
                for prefix in ("language-", "lang-", "highlight-"):
                    if token.startswith(prefix):
                        language = token[len(prefix) :]
                        break
                if language is not None:
                    break
            if language is not None:
                break

        # Per-line offsets в исходнике дать невозможно: lxml даёт sourceline
        # только всего <pre>/<code>, а не каждой строки кода. Оставляем пустым
        # — chunker деградирует на atomic-or-overflow без line-split.
        content = _serialize(el)
        return HtmlCodeBlockSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
            language=language,
            code=code,
            code_line_locations=(),
        )

    @staticmethod
    def _parse_blockquote(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        content = _serialize(el)
        text = _inline_markdown(el)
        return HtmlBlockquoteSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
            text=text,
        )

    @staticmethod
    def _parse_hr(
        ctx: _ParseContext,
        el: _Element,
        order: int,
        start: int,
        end: int,
    ) -> _ParseResult:
        content = _serialize(el)
        return HtmlHorizontalRuleSection(
            source_id=ctx.source_id,
            content=content,
            order=order,
            metadata=_with_location(ctx.base_metadata, start, end),
        )
