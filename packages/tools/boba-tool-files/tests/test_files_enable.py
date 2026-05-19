"""Тесты enable-конвенции и allowlist'а files-плагина v2."""

from __future__ import annotations

from collections.abc import Callable


_ALL_FILE_TOOLS = {
    "append", "cat", "cd", "cp", "edit", "grep", "ls", "mkdir",
    "mv", "pwd", "rm", "stat", "touch", "tree", "write",
}


def test_disabled_by_default(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    assert make_files_tool_names({}) == []


def test_disabled_explicit(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    assert make_files_tool_names({"BOBA_TOOL__FILES__ENABLE": "false"}) == []


def test_enabled_yields_all_tools(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    names = make_files_tool_names({"BOBA_TOOL__FILES__ENABLE": "true"})
    assert set(names) == _ALL_FILE_TOOLS


def test_allowlist_filters_tools(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    names = make_files_tool_names({
        "BOBA_TOOL__FILES__ENABLE": "true",
        "BOBA_TOOL__FILES__TOOLS": "cat,grep",
    })
    assert set(names) == {"cat", "grep"}


def test_unknown_allowlist_name_is_silently_dropped(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    """Несуществующие имена в allowlist'е просто не матчатся — без падения.

    Это поведение v2: enable_if-predicate возвращает False для
    неизвестных имён. Trade-off против v1-поведения (там было ValueError):
    конфиг остаётся загружаемым, оператор обнаруживает опечатку через
    отсутствие tool'а в каталоге.
    """
    names = make_files_tool_names({
        "BOBA_TOOL__FILES__ENABLE": "true",
        "BOBA_TOOL__FILES__TOOLS": "cat,nonsense",
    })
    assert set(names) == {"cat"}
