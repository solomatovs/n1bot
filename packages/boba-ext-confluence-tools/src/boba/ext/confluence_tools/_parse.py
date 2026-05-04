"""Workspace-bridge для confluence-shared parser'а: load_soup(workspace, path)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from boba.confluence_shared import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
    resolve_anchor,
    strip_confluence_macros,
)
from boba.workspace import WorkspaceShell

__all__ = [
    "Heading",
    "anchor_for",
    "collect_headings",
    "load_soup",
    "resolve_anchor",
    "strip_confluence_macros",
]


def load_soup(workspace: WorkspaceShell, path: str) -> BeautifulSoup:
    """Прочитать HTML из workspace и распарсить через lxml."""
    with workspace.read_binary(path) as f:
        data = f.read()
    return parse_html(data)
