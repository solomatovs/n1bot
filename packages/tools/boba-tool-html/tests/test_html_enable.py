"""Тесты config-gated загрузки html-плагина через discover_plugins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def test_disabled_by_default(
    make_html_tool_names: Callable[[dict[str, Any]], list[str]],
):
    assert make_html_tool_names({}) == []


def test_disabled_explicit(
    make_html_tool_names: Callable[[dict[str, Any]], list[str]],
):
    assert make_html_tool_names({"enable": False}) == []


def test_enabled_yields_both_tools(
    make_html_tool_names: Callable[[dict[str, Any]], list[str]],
):
    names = make_html_tool_names({"enable": True})
    assert set(names) == {"html_outline", "html_section"}


def test_allowlist_filters_tools(
    make_html_tool_names: Callable[[dict[str, Any]], list[str]],
):
    names = make_html_tool_names({"enable": True, "tools": ["html_outline"]})
    assert set(names) == {"html_outline"}
