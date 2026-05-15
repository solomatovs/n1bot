"""Тесты enable-конвенции и сборки HtmlPlugin."""

from __future__ import annotations

from collections.abc import Callable


def test_disabled_by_default(
    make_html_tool_names: Callable[[dict[str, str]], list[str]],
):
    assert make_html_tool_names({}) == []


def test_disabled_explicit(
    make_html_tool_names: Callable[[dict[str, str]], list[str]],
):
    assert make_html_tool_names({"BOBA_TOOL__HTML__ENABLE": "false"}) == []


def test_enabled_yields_both_tools(
    make_html_tool_names: Callable[[dict[str, str]], list[str]],
):
    names = make_html_tool_names({"BOBA_TOOL__HTML__ENABLE": "true"})
    assert set(names) == {"html_outline", "html_section"}


def test_allowlist_filters_tools(
    make_html_tool_names: Callable[[dict[str, str]], list[str]],
):
    names = make_html_tool_names(
        {
            "BOBA_TOOL__HTML__ENABLE": "true",
            "BOBA_TOOL__HTML__TOOLS": "html_outline",
        },
    )
    assert set(names) == {"html_outline"}
