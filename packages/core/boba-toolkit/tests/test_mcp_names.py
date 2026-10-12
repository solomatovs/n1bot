"""Имена инструментов серверов MCP: полное имя и разбор обратно."""

from __future__ import annotations

import pytest

from boba.toolkit.mcp_names import McpToolName, McpToolNameError, McpToolNames


class TestMcpToolNames:
    def test_full_name_carries_the_server_and_the_tool(self) -> None:
        full = McpToolNames().full("boba", "pg_query")

        if full != "mcp__boba__pg_query":
            raise AssertionError(f"полное имя по форме, дано {full!r}")

    def test_full_name_parses_back_into_server_and_tool(self) -> None:
        names = McpToolNames()

        parsed = names.parse(names.full("deep-wiki", "ask_question"))

        if parsed != McpToolName(server="deep-wiki", tool="ask_question"):
            raise AssertionError(f"сервер и инструмент обратно, дано {parsed!r}")

    def test_separator_inside_the_tool_name_stays_in_the_tool(self) -> None:
        names = McpToolNames()

        parsed = names.parse(names.full("boba", "kb__fts__search"))

        if parsed != McpToolName(server="boba", tool="kb__fts__search"):
            raise AssertionError(f"`__` остаётся в имени инструмента, дано {parsed!r}")

    def test_tool_name_starting_with_underscore_survives_the_round_trip(self) -> None:
        names = McpToolNames()

        parsed = names.parse(names.full("boba", "_hidden"))

        if parsed != McpToolName(server="boba", tool="_hidden"):
            raise AssertionError(f"подчёркивание в начале имени, дано {parsed!r}")

    def test_same_tool_of_two_servers_gets_different_full_names(self) -> None:
        names = McpToolNames()

        if names.full("first", "echo") == names.full("second", "echo"):
            raise AssertionError("одноимённые инструменты серверов не различаются")

    @pytest.mark.parametrize("name", ["Skill", "pg_query", "mcp", "mcp_boba__x", ""])
    def test_own_tool_name_is_not_an_mcp_tool(self, name: str) -> None:
        parsed = McpToolNames().parse(name)

        if parsed is not None:
            raise AssertionError(f"{name!r} не инструмент сервера MCP, дано {parsed!r}")

    @pytest.mark.parametrize("name", ["mcp__boba", "mcp__boba__", "mcp____tool"])
    def test_incomplete_full_name_is_not_an_mcp_tool(self, name: str) -> None:
        parsed = McpToolNames().parse(name)

        if parsed is not None:
            raise AssertionError(f"{name!r} не разбирается, дано {parsed!r}")

    def test_server_name_with_separator_is_refused(self) -> None:
        with pytest.raises(McpToolNameError, match=r"'my__srv'.*contains '__'"):
            McpToolNames().full("my__srv", "echo")

    def test_empty_server_name_is_refused(self) -> None:
        with pytest.raises(McpToolNameError, match=r"it is empty"):
            McpToolNames().full("", "echo")

    def test_server_name_ending_with_underscore_is_refused(self) -> None:
        with pytest.raises(McpToolNameError, match=r"'srv_'.*ends with '_'"):
            McpToolNames().checked_server("srv_")

    def test_empty_tool_name_is_refused(self) -> None:
        with pytest.raises(McpToolNameError, match=r"'boba'.*empty name"):
            McpToolNames().full("boba", "")

    def test_server_prefix_starts_every_tool_name_of_the_server(self) -> None:
        names = McpToolNames()

        prefix = names.server_prefix("boba")

        if prefix != "mcp__boba__":
            raise AssertionError(f"начало имён сервера, дано {prefix!r}")

        if not names.full("boba", "shell").startswith(prefix):
            raise AssertionError("полное имя не начинается с приставки сервера")

    def test_tool_of_drops_the_server_and_keeps_an_own_name(self) -> None:
        names = McpToolNames()

        if names.tool_of("mcp__boba__pg_query") != "pg_query":
            raise AssertionError("имя инструмента сервера без сервера")

        if names.tool_of("Skill") != "Skill":
            raise AssertionError("собственное имя остаётся как есть")

    def test_node_tool_is_named_fully_from_either_form(self) -> None:
        names = McpToolNames()

        if names.in_server("boba", "pg_query") != "mcp__boba__pg_query":
            raise AssertionError("имя на сервере получает сервер")

        if names.in_server("boba", "mcp__boba__pg_query") != "mcp__boba__pg_query":
            raise AssertionError("полное имя своего сервера остаётся собой")

        if names.in_server("boba", "mcp__other__x") != "mcp__boba__mcp__other__x":
            raise AssertionError("имя чужого сервера — имя инструмента на своём")
