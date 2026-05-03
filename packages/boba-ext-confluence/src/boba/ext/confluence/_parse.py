"""Парсинг Confluence-export'ов: anchor'ы из ac:structured-macro, очистка макросов."""

from __future__ import annotations

from dataclasses import dataclass

from boba.workspace import WorkspaceShell
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Служебный браузерный anchor; никогда не бывает осмысленной целью навигации.
_GOBACK = "_GoBack"


@dataclass(frozen=True)
class Heading:
    """Заголовок Confluence-страницы; index 1-based, anchor — scroll-bookmark-N."""

    index: int
    level: int
    text: str
    anchor: str | None
    tag: Tag


def load_soup(workspace: WorkspaceShell, path: str) -> BeautifulSoup:
    """Прочитать HTML из workspace и распарсить через lxml."""
    with workspace.read_binary(path) as f:
        data = f.read()
    return BeautifulSoup(data, "lxml")


def collect_headings(
    soup: BeautifulSoup,
    max_depth: int | None = None,
) -> list[Heading]:
    """Все <h1>..<h6> в document order; max_depth ограничивает уровень."""
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
            )
        )
    return headings


def anchor_for(h: Heading) -> str:
    """Канонический anchor: scroll-bookmark-N или idx:N если нет confluence-anchor'а."""
    return h.anchor if h.anchor else f"idx:{h.index}"


def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
    """Найти heading по anchor: confluence-имя или idx:N (с/без ведущего #)."""
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


def strip_confluence_macros(html: str) -> str:
    """Вырезать ac:*/ri:* теги из HTML; текстовые children сохраняются."""
    soup = BeautifulSoup(html, "lxml")
    for el in list(soup.find_all(_is_confluence_macro)):
        el.decompose()
    body = soup.body
    if body is None:
        return str(soup)
    return body.decode_contents()


def _is_confluence_macro(tag: Tag) -> bool:
    name = getattr(tag, "name", None)
    return bool(name and name.startswith(("ac:", "ri:")))


def _heading_anchor(tag: Tag) -> str | None:
    """Первый осмысленный confluence-anchor внутри heading; fallback — html id."""
    for sm in tag.find_all(
        lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor"
    ):
        param = sm.find("ac:parameter")
        if param is None:
            continue
        name = param.get_text(strip=True)
        if name and name != _GOBACK:
            return name
    raw = tag.get("id")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return str(raw) if raw else None


def _heading_text(tag: Tag) -> str:
    """Текст heading без содержимого confluence-макросов."""
    parts: list[str] = []
    for el in tag.descendants:
        if not isinstance(el, NavigableString):
            continue
        if any(_is_confluence_macro(p) for p in el.parents):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())
