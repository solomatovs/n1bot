"""Тесты enable-конвенции и сборки FilesPlugin."""

from __future__ import annotations

from collections.abc import Callable

import pytest

_FILES_TOOL_NAMES = {
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
    assert set(names) == _FILES_TOOL_NAMES


def test_allowlist_filters_tools(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    names = make_files_tool_names(
        {
            "BOBA_TOOL__FILES__ENABLE": "true",
            "BOBA_TOOL__FILES__TOOLS": "cat,grep",
        },
    )
    assert set(names) == {"cat", "grep"}


def test_allowlist_unknown_name_raises(
    make_files_tool_names: Callable[[dict[str, str]], list[str]],
):
    with pytest.raises(ValueError, match="unknown names"):
        make_files_tool_names(
            {
                "BOBA_TOOL__FILES__ENABLE": "true",
                "BOBA_TOOL__FILES__TOOLS": "cat,nonsense",
            },
        )
