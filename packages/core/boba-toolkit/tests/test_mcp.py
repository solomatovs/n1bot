"""Инструменты серверов MCP: пометки карточки и ответ сервера, который клиент
MCP не получает от сервера стенда."""

from __future__ import annotations

import mcp_types as mt
import pytest

from boba.toolkit.dag import ToolHints
from boba.toolkit.mcp import McpFailure, McpResults, McpToolStubs
from boba.toolkit.result import ErrorResult


@pytest.mark.parametrize(
    "hints",
    [
        ToolHints(),
        ToolHints(read_only=True, destructive=False),
        ToolHints(read_only=False, destructive=False),
    ],
)
def test_hints_survive_the_round_trip_through_annotations(hints: ToolHints) -> None:
    stubs = McpToolStubs()
    tool = mt.Tool(
        name="add",
        input_schema={"type": "object"},
        annotations=stubs.annotations_of(hints),
    )

    if stubs.hints_of(tool) != hints:
        raise AssertionError(f"пометки туда и обратно, дано {stubs.hints_of(tool)}")


def test_input_required_is_an_error_for_the_model() -> None:
    reply = mt.InputRequiredResult(request_state="opaque")

    result = McpResults().result("mcp__boba__ask", "call-1", reply)

    if not isinstance(result, ErrorResult):
        raise AssertionError(f"просьба о вводе — ошибка, дано {result!r}")
    if result.error_kind != McpFailure.TOOL_ERROR:
        raise AssertionError(f"вид отказа — ошибка инструмента, дано {result!r}")
    if "input_required" not in result.message:
        raise AssertionError(f"причина в тексте, дано {result.message!r}")
    if "'call-1'" not in result.message:
        raise AssertionError(f"вызов в тексте, дано {result.message!r}")
