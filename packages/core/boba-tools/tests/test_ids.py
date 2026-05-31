"""Тесты домена идентификаторов tool'ов."""

import pytest

from boba.tools.domain.ids import (
    ToolName,
    sanitize_source_id,
    to_tool_id,
)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("boba.tools.plugins.html", "boba_tools_plugins_html"),
        ("inline", "inline"),
        ("mcp-github", "mcp-github"),
        ("123abc", "123abc"),
        ("__main__", "main"),  # `__` схлопывается, ведущие/хвостовые `_` режутся
        ("a__b", "a_b"),
        ("a_", "a"),
        ("-x-", "x"),
        ("", "plugin"),  # пустой fallback
        ("...", "plugin"),
    ],
)
def test_sanitize_source_id(origin: str, expected: str) -> None:
    assert sanitize_source_id(origin) == expected


@pytest.mark.parametrize(
    "name",
    ["outline", "cat", "kb_search", "tool-1", "a__b"],
)
def test_to_tool_id_passes_valid_names(name: str) -> None:
    # wire-имя = само имя tool'а, без префикса источника.
    assert to_tool_id(ToolName(name)) == name


@pytest.mark.parametrize(
    "name",
    ["", "_leading", "-leading", "has space", "has.dot", "юникод"],
)
def test_to_tool_id_rejects_bad_charset(name: str) -> None:
    with pytest.raises(ValueError):
        to_tool_id(ToolName(name))


def test_to_tool_id_rejects_too_long() -> None:
    with pytest.raises(ValueError, match="max 64"):
        to_tool_id(ToolName("a" * 65))
