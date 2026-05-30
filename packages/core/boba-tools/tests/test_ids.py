"""Тесты домена идентификаторов tool'ов."""

import pytest

from boba.tools.domain.ids import (
    ToolName,
    compose_tool_id,
    parse_tool_id,
    sanitize_source_id,
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
    "origin",
    ["boba.tools.html", "inline", "__main__", "a__b", "a_", "-x-", "", "..."],
)
def test_sanitized_source_id_round_trips(origin: str) -> None:
    sid = sanitize_source_id(origin)
    tool_id = compose_tool_id(sid, ToolName("mytool"))
    assert parse_tool_id(tool_id) == (sid, ToolName("mytool"))
