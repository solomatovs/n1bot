"""
Markdown heading-aware парсер

Поддерживает ATX-style heading (`# Title`, `## Subtitle`, ...).

Setext-heading (`====` / `----` под строкой) не поддерживаются — для целей
индексации они почти не встречаются и обрабатываются как plain text

Code-fence (```...```) и блоки кода (4 пробела отступа) пропускаются —
символы `#` внутри них не считаются heading

Anchor — slug из текста heading (lowercase, пробелы → '-', не-alnum
выкидываются). Fallback `idx:N`
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Heading",
    "MarkdownSection",
    "anchor_for",
    "collect_headings",
    "resolve_anchor",
    "slugify",
    "split_sections",
]

# ATX heading: 1..6 решётки + пробел + текст. Опциональный закрывающий ###.
_ATX = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
_FENCE = re.compile(r"^(```|~~~)")
_NONALNUM = re.compile(r"[^\w\-]+", flags=re.UNICODE)


@dataclass(frozen=True)
class Heading:
    """Markdown ATX heading
        index 1-based
        line  0-based номер строки начала
    """

    index: int
    level: int
    text: str
    anchor: str | None
    line: int


@dataclass(frozen=True)
class MarkdownSection:
    """
    Раздел markdown-документа: заголовок + body до следующего heading
    """

    heading: Heading | None     # None означает что это преамбула до первого heading
    body: str                   # текст после строки heading до следующего heading


def collect_headings(text: str, *, max_depth: int | None = None) -> list[Heading]:
    """
    Все ATX heading документа в порядке появления

    Heading внутри code-fence (```...```) пропускаются
    `max_depth` ограничивает максимальный уровень (1..max_depth)
    """
    cap = max_depth if max_depth is not None else 6
    in_fence = False
    out: list[Heading] = []
    idx = 0
    for line_no, line in enumerate(text.splitlines()):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _ATX.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if level > cap:
            continue
        idx += 1
        title = m.group(2).strip()
        out.append(
            Heading(
                index=idx,
                level=level,
                text=title,
                anchor=slugify(title),
                line=line_no,
            )
        )
    return out


def split_sections(
    text: str, *, max_depth: int | None = None
) -> list[MarkdownSection]:
    """
    Разбить markdown на MarkdownSection по heading

    `MarkdownSection.body` — строки **после** heading-строки до следующего
    heading (включая пустые), без trailing whitespace.
    Если до первого heading есть текст (preamble),
    он становится секцией с heading=None

    Heading внутри code-fence пропускаются (см. collect_headings)
    """
    headings = collect_headings(text, max_depth=max_depth)
    lines = text.splitlines()
    sections: list[MarkdownSection] = []

    if not headings:
        body = text.strip()
        if body:
            sections.append(MarkdownSection(heading=None, body=body))
        return sections

    # Preamble: текст до первой heading-строки.
    if headings[0].line > 0:
        preamble = "\n".join(lines[: headings[0].line]).strip()
        if preamble:
            sections.append(MarkdownSection(heading=None, body=preamble))

    for i, h in enumerate(headings):
        end_line = headings[i + 1].line if i + 1 < len(headings) else len(lines)
        body_lines = lines[h.line + 1 : end_line]
        body = "\n".join(body_lines).strip()
        sections.append(MarkdownSection(heading=h, body=body))
    return sections


def anchor_for(h: Heading) -> str:
    """Канонический anchor: slug или idx:N если slug пуст."""
    return h.anchor if h.anchor else f"idx:{h.index}"


def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
    """Найти heading по slug или idx:N (с/без ведущего #)."""
    key = anchor.lstrip("#").strip()
    if key.startswith("idx:"):
        try:
            idx = int(key[4:])
        except ValueError:
            return None
        for h in headings:
            if h.index == idx:
                return h
        return None
    for h in headings:
        if h.anchor == key:
            return h
    return None


def slugify(text: str) -> str | None:
    """`Foo Bar! 123` → `foo-bar-123`. Пусто → None."""
    s = text.strip().lower().replace(" ", "-")
    s = _NONALNUM.sub("", s)
    s = s.strip("-")
    return s or None
