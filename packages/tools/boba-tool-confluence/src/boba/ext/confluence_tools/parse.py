"""Confluence-specific HTML parser: ac:*/ri:* macros + scroll-bookmark anchors.

Self-contained: BeautifulSoup-based heading-aware extraction, без зависимостей
на `boba.html` (там новый lxml structural parser, который не подходит для
Confluence-export'а — там кастомные namespaces `ac:`/`ri:`, которые удобнее
обходить через bs4).

Содержимое:

- `Heading`                     — DTO заголовка (`level`, `text`, `anchor`).
- `parse_html`                  — `BeautifulSoup` поверх lxml-парсера.
- `collect_headings`            — все `<h1>..<h6>` с anchor'ом из
  Confluence scroll-bookmark или html-id; текст без ac:*/ri:* поддеревьев.
- `text_between`                — конкатенация NavigableString между двумя
  тегами с пропуском ac:*/ri:* содержимого.
- `plain_text`                  — текст всего поддерева с пропуском
  ac:*/ri:* содержимого.
- `anchor_for`                  — canonical-anchor (html-id или `idx:N`).
- `resolve_anchor`              — resolver для deep-link.
- `strip_confluence_macros`     — вырезать ac:*/ri:* поддеревья из HTML-строки.
- `is_confluence_macro`         — predicate для bs4-фильтрации.
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

__all__ = [
    "Heading",
    "anchor_for",
    "collect_headings",
    "is_confluence_macro",
    "parse_html",
    "plain_text",
    "resolve_anchor",
    "strip_confluence_macros",
    "text_between",
]

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_HEADING_TAG_NAMES = frozenset(_HEADING_TAGS)

# Служебный браузерный anchor (Confluence ставит его в начало страницы при
# экспорте; ни на что конкретное не ссылается). Игнорируем при extraction'е,
# чтобы не плодить бесполезные anchor'ы.
_GOBACK = "_GoBack"


@dataclass(frozen=True)
class Heading:
    """Заголовок Confluence-страницы. `index` 1-based; `anchor` —
    scroll-bookmark или html `id`, `None` если ни того, ни другого.
    """

    index: int
    level: int
    text: str
    anchor: str | None
    tag: Tag


def parse_html(data: bytes | str) -> BeautifulSoup:
    """BeautifulSoup поверх lxml — терпит произвольные namespace'ы (ac:/ri:)."""
    return BeautifulSoup(data, "lxml")


def collect_headings(
    soup: BeautifulSoup,
    *,
    max_depth: int | None = None,
) -> list[Heading]:
    """Все `<h1>..<h6>` в document order. Текст и anchor — Confluence-aware."""
    cap = max_depth if max_depth is not None else 6
    tags = soup.find_all(list(_HEADING_TAGS[:cap]))
    headings: list[Heading] = []
    for i, tag in enumerate(tags, start=1):
        level = int(tag.name[1])
        headings.append(
            Heading(
                index=i,
                level=level,
                text=_heading_text(tag),
                anchor=_heading_anchor(tag),
                tag=tag,
            ),
        )
    return headings


def anchor_for(h: Heading) -> str:
    """Canonical-anchor: html-id/scroll-bookmark или `idx:N` если ни того, ни другого."""
    return h.anchor if h.anchor else f"idx:{h.index}"


def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
    """Найти heading по anchor: html-id/scroll-bookmark или `idx:N`
    (с/без ведущего `#`).
    """
    key = anchor.lstrip("#").strip()
    if key.startswith("idx:"):
        try:
            idx = int(key[4:])
        except ValueError:
            return None
        return next((h for h in headings if h.index == idx), None)
    return next((h for h in headings if h.anchor == key), None)


def text_between(start_tag: Tag, end_tag: Tag | None) -> str:
    """Конкатенация всех `NavigableString` между `start_tag` (исключая) и
    `end_tag`, с пропуском содержимого ac:*/ri:* макросов и текста самих
    heading-тегов (он живёт в `Heading.text`).
    """
    parts: list[str] = []
    for el in start_tag.next_elements:
        if el is end_tag:
            break
        if not isinstance(el, NavigableString):
            continue
        if _is_inside_heading(el):
            continue
        if _is_inside_macro(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def plain_text(node: Tag) -> str:
    """Plain-text всего поддерева, с пропуском ac:*/ri:* содержимого."""
    parts: list[str] = []
    for el in node.descendants:
        if not isinstance(el, NavigableString):
            continue
        if _is_inside_macro(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def strip_confluence_macros(html: str) -> str:
    """Вырезать ac:*/ri:* поддеревья из HTML-строки (целиком, с children)."""
    soup = parse_html(html)
    for el in list(soup.find_all(is_confluence_macro)):
        el.decompose()
    body = soup.body
    if body is None:
        return str(soup)
    return body.decode_contents()


def is_confluence_macro(tag: object) -> bool:
    """True если тег принадлежит Confluence-расширениям (ac:* / ri:*)."""
    name = getattr(tag, "name", None)
    return bool(name and name.startswith(("ac:", "ri:")))


def _heading_anchor(tag: Tag) -> str | None:
    """Anchor heading'а: scroll-bookmark из ac:structured-macro или html-id."""
    for sm in tag.find_all(
        lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor",
    ):
        param = sm.find("ac:parameter")
        if param is None:
            continue
        name = param.get_text(strip=True)
        if name and name != _GOBACK:
            return name
    return _extract_html_id(tag)


def _heading_text(tag: Tag) -> str:
    """Текст heading'а — все NavigableString рекурсивно, без ac:*/ri:*."""
    parts: list[str] = []
    for el in tag.descendants:
        if not isinstance(el, NavigableString):
            continue
        if any(isinstance(p, Tag) and is_confluence_macro(p) for p in el.parents):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def _extract_html_id(tag: Tag) -> str | None:
    """Достать html `id` heading-тега; пустой / list → None."""
    raw = tag.get("id")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return str(raw) if raw else None


def _is_inside_heading(el: NavigableString) -> bool:
    return any(
        isinstance(p, Tag) and p.name in _HEADING_TAG_NAMES for p in el.parents
    )


def _is_inside_macro(el: NavigableString) -> bool:
    return any(
        isinstance(p, Tag) and is_confluence_macro(p) for p in el.parents
    )
