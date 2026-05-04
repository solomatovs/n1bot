"""Confluence-specific HTML parser: ac:*/ri:* macros + scroll-bookmark anchors.

Поверх generic `boba-ext-html-parser`: переопределяет извлечение текста и
anchor'а, чтобы знать про Confluence-специфику:

- `_heading_anchor`: предпочитает `<ac:structured-macro ac:name="anchor">
  <ac:parameter>scroll-bookmark-N</ac:parameter></ac:structured-macro>`,
  иначе fallback на html `id`.
- Текст heading'ов и текст между ними не включает содержимое ac:*/ri:* тегов
  (служебная разметка экспорта, не контент).
- `strip_confluence_macros` вырезает всё ac:*/ri:* поддерево.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from boba.ext.html_parser import (
    Heading,
    anchor_for,
    extract_html_id,
    heading_default_text_skip,
    parse_html,
    resolve_anchor,
)
from boba.ext.html_parser import (
    collect_headings as _collect_headings_generic,
)
from boba.ext.html_parser import (
    plain_text as _plain_text_generic,
)
from boba.ext.html_parser import (
    text_between as _text_between_generic,
)

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

# Служебный браузерный anchor; никогда не бывает осмысленной целью навигации.
_GOBACK = "_GoBack"


def is_confluence_macro(tag: object) -> bool:
    """True если тег принадлежит Confluence-расширениям (ac:* / ri:*)."""
    name = getattr(tag, "name", None)
    return bool(name and name.startswith(("ac:", "ri:")))


def collect_headings(
    soup: BeautifulSoup, max_depth: int | None = None
) -> list[Heading]:
    """Heading'и Confluence-страницы; anchor — scroll-bookmark или html-id."""
    return _collect_headings_generic(
        soup,
        max_depth=max_depth,
        text_extractor=_heading_text,
        anchor_extractor=_heading_anchor,
    )


def text_between(start_tag: Tag, end_tag: Tag | None) -> str:
    """text_between с пропуском содержимого ac:*/ri:* макросов."""
    return _text_between_generic(
        start_tag, end_tag, skip_string=_inside_macro,
    )


def plain_text(node: Tag) -> str:
    """plain_text поддерева с пропуском ac:*/ri:* содержимого."""
    return _plain_text_generic(node, skip_string=_inside_macro)


def strip_confluence_macros(html: str) -> str:
    """Вырезать ac:*/ri:* теги из HTML; текстовые children НЕ сохраняются."""
    soup = parse_html(html)
    for el in list(soup.find_all(is_confluence_macro)):
        el.decompose()
    body = soup.body
    if body is None:
        return str(soup)
    return body.decode_contents()


def _heading_anchor(tag: Tag) -> str | None:
    """Confluence scroll-bookmark внутри heading; fallback — html id."""
    for sm in tag.find_all(
        lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor"
    ):
        param = sm.find("ac:parameter")
        if param is None:
            continue
        name = param.get_text(strip=True)
        if name and name != _GOBACK:
            return name
    return extract_html_id(tag)


def _heading_text(tag: Tag) -> str:
    """Текст heading'а без содержимого Confluence-макросов."""
    return heading_default_text_skip(tag, skip_when=is_confluence_macro)


def _inside_macro(el: NavigableString) -> bool:
    return any(
        isinstance(p, Tag) and is_confluence_macro(p) for p in el.parents
    )
