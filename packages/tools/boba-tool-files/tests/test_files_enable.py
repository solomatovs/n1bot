"""Тесты config-gated загрузки files-плагина через discover_plugins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_ALL_FILE_TOOLS = {
    "append", "cat", "cd", "cp", "edit", "grep", "ls", "mkdir",
    "mv", "pwd", "read_bytes", "rm", "stat", "touch", "tree", "unzip", "write",
}


def test_disabled_by_default(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    """Пустая секция -> плагин не грузится."""
    assert make_files_tool_names({}) == []


def test_disabled_explicit(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    assert make_files_tool_names({"enable": False}) == []


def test_enabled_yields_all_tools(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    names = make_files_tool_names({"enable": True})
    assert set(names) == _ALL_FILE_TOOLS


def test_allowlist_filters_tools(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    names = make_files_tool_names({"enable": True, "tools": ["cat", "grep"]})
    assert set(names) == {"cat", "grep"}


def test_csv_string_allowlist(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    """CSV-строка (имитирует env-var вход) корректно парсится в список."""
    names = make_files_tool_names({"enable": True, "tools": "cat,grep"})
    assert set(names) == {"cat", "grep"}


def test_unknown_allowlist_name_is_silently_dropped(
    make_files_tool_names: Callable[[dict[str, Any]], list[str]],
):
    """Несуществующие имена в allowlist'е просто не матчатся — без падения."""
    names = make_files_tool_names({"enable": True, "tools": ["cat", "nonsense"]})
    assert set(names) == {"cat"}
