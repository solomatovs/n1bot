"""Тесты config-gated загрузки doc-плагина через discover_plugins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def test_disabled_by_default(
    make_doc_tool_names: Callable[[dict[str, Any]], list[str]],
):
    """Пустая секция → плагин не грузится."""
    assert make_doc_tool_names({}) == []


_ALL_DOC_TOOLS = {
    "document_outline",
    "read_document",
    "read_document_window",
    "read_pages",
    "search_document",
}


def test_enabled_yields_all_tools(
    make_doc_tool_names: Callable[[dict[str, Any]], list[str]],
):
    assert set(make_doc_tool_names({"enable": True})) == _ALL_DOC_TOOLS


def test_allowlist_filters_tools(
    make_doc_tool_names: Callable[[dict[str, Any]], list[str]],
):
    names = make_doc_tool_names(
        {"enable": True, "tools": ["read_pages", "search_document"]},
    )
    assert set(names) == {"read_pages", "search_document"}
    assert make_doc_tool_names({"enable": True, "tools": ["nonsense"]}) == []
