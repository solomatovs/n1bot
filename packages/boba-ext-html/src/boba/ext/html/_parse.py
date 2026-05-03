"""Общие helper'ы парсинга HTML для outline/section tools."""

from __future__ import annotations

from dataclasses import dataclass

from boba.workspace import WorkspaceShell
from bs4 import BeautifulSoup
from bs4.element import Tag

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


@dataclass(frozen=True)
class Heading:
    """Один заголовок в порядке появления; index 1-based."""

    index: int
    level: int
    text: str
    html_id: str | None
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
        text = " ".join(tag.get_text().split())
        headings.append(
            Heading(
                index=i,
                level=level,
                text=text,
                html_id=_first_id(tag),
                tag=tag,
            )
        )
    return headings


def anchor_for(h: Heading) -> str:
    """Канонический anchor для html_section: id или idx:N."""
    return h.html_id if h.html_id else f"idx:{h.index}"


def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
    """Найти heading по anchor: <id> или idx:N (с/без ведущего #)."""
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
        if h.html_id == key:
            return h
    return None


def _first_id(tag: Tag) -> str | None:
    raw = tag.get("id")
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw[0] if raw else None
    return str(raw)
