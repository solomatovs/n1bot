"""Generic HTML heading-aware parser.

Без I/O — работает с готовыми bytes/str/BeautifulSoup. Не знает специфики
конкретных HTML-диалектов (Confluence, MediaWiki и т.п.). Анкор берётся из
html-атрибута `id` heading-тега; если нет — fallback `idx:N` через
`anchor_for(h)`.

Расширения для конкретных диалектов (Confluence `ac:structured-macro` и
т.п.) живут в отдельных пакетах поверх этого: они переопределяют
`_heading_anchor` и/или фильтруют диалект-специфичные теги.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

__all__ = [
    "Heading",
    "anchor_for",
    "collect_headings",
    "extract_html_id",
    "heading_default_text",
    "heading_default_text_skip",
    "is_inside_heading",
    "parse_html",
    "plain_text",
    "resolve_anchor",
    "text_between",
]

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_HEADING_TAG_NAMES = frozenset(_HEADING_TAGS)


@dataclass(frozen=True)
class Heading:
    """Заголовок HTML-документа; index 1-based, anchor — html `id` или None."""

    index: int
    level: int
    text: str
    anchor: str | None
    tag: Tag


def parse_html(data: bytes | str) -> BeautifulSoup:
    """BeautifulSoup поверх lxml — терпит произвольные namespace'ы."""
    return BeautifulSoup(data, "lxml")


def collect_headings(
    soup: BeautifulSoup,
    *,
    max_depth: int | None = None,
    text_extractor: Callable[[Tag], str] | None = None,
    anchor_extractor: Callable[[Tag], str | None] | None = None,
) -> list[Heading]:
    """Все <h1>..<h6> в document order.

    `max_depth` — ограничение по уровню (по умолчанию все 6).
    `text_extractor` / `anchor_extractor` — переопределяемые стратегии для
    диалектов (Confluence-shared передаёт свои с обработкой ac:* тегов).
    Default — generic: текст всех NavigableString, anchor — html `id`.
    """
    cap = max_depth if max_depth is not None else 6
    tags = soup.find_all(list(_HEADING_TAGS[:cap]))
    text_fn = text_extractor or heading_default_text
    anchor_fn = anchor_extractor or extract_html_id
    headings: list[Heading] = []
    for i, tag in enumerate(tags, start=1):
        level = int(tag.name[1])
        headings.append(
            Heading(
                index=i,
                level=level,
                text=text_fn(tag),
                anchor=anchor_fn(tag),
                tag=tag,
            )
        )
    return headings


def anchor_for(h: Heading) -> str:
    """Канонический anchor: html-id или idx:N если id не задан."""
    return h.anchor if h.anchor else f"idx:{h.index}"


def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
    """Найти heading по anchor: html-id или idx:N (с/без ведущего #)."""
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


def extract_html_id(tag: Tag) -> str | None:
    """Достать html `id` heading-тега; пустой/list → None."""
    raw = tag.get("id")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return str(raw) if raw else None


def heading_default_text(tag: Tag) -> str:
    """Текст heading'а — все NavigableString рекурсивно."""
    return heading_default_text_skip(tag, skip_when=lambda _: False)


def heading_default_text_skip(
    tag: Tag,
    *,
    skip_when: Callable[[Tag], bool],
) -> str:
    """Текст heading'а с фильтром: skip_when(parent) → пропустить эту строку.

    Используется диалектами для исключения текста внутри собственных
    спец-тегов (Confluence ac:* / ri:*).
    """
    parts: list[str] = []
    for el in tag.descendants:
        if not isinstance(el, NavigableString):
            continue
        if any(isinstance(p, Tag) and skip_when(p) for p in el.parents):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def is_inside_heading(el: NavigableString) -> bool:
    """True если NavigableString находится внутри h1..h6."""
    return any(
        isinstance(p, Tag) and p.name in _HEADING_TAG_NAMES
        for p in el.parents
    )


def text_between(
    start_tag: Tag,
    end_tag: Tag | None,
    *,
    skip_string: Callable[[NavigableString], bool] | None = None,
) -> str:
    """Конкатенация всех NavigableString между start_tag (исключая) и end_tag.

    Текст самих heading-тегов всегда пропускается (он живёт в Heading.text).
    `skip_string` — дополнительный фильтр диалекта (например, текст внутри
    Confluence ac:*/ri: макросов).
    """
    parts: list[str] = []
    for el in start_tag.next_elements:
        if el is end_tag:
            break
        if not isinstance(el, NavigableString):
            continue
        if is_inside_heading(el):
            continue
        if skip_string is not None and skip_string(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def plain_text(
    node: Tag,
    *,
    skip_string: Callable[[NavigableString], bool] | None = None,
) -> str:
    """Текст всего поддерева, с опциональным фильтром на NavigableString."""
    parts: list[str] = []
    for el in node.descendants:
        if not isinstance(el, NavigableString):
            continue
        if skip_string is not None and skip_string(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def _is_descendants(parents: Iterable[Tag], names: tuple[str, ...]) -> bool:
    """True если хоть один parent среди заданных имён (helper)."""
    return any(isinstance(p, Tag) and p.name in names for p in parents)
