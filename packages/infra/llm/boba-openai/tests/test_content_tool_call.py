"""Тесты ContentToolCallDecoder: перемап tool-call из content в tool_calls."""

from __future__ import annotations

import json

from boba.llm.models import AssistantMessage, ToolCall
from boba.provider.openai.response import ToolCallFromContentFallback


def _decode(content: str = "", **fields: object) -> AssistantMessage:
    message = AssistantMessage(content=content, **fields)  # type: ignore[arg-type]
    return ToolCallFromContentFallback().decode(message, model="test-model")


def test_single_object_remapped() -> None:
    content = json.dumps({"function": "search", "args": {"q": "x"}})
    out = _decode(content)

    assert out.content == ""
    assert len(out.tool_calls) == 1
    call = out.tool_calls[0]
    assert call.name == "search"
    assert call.args == {"q": "x"}
    assert call.id


def test_array_remapped_in_order() -> None:
    content = json.dumps(
        [
            {"function": "a", "args": {"x": 1}},
            {"function": "b", "args": {}},
        ]
    )
    out = _decode(content)

    assert [c.name for c in out.tool_calls] == ["a", "b"]
    assert out.content == ""


def test_invalid_json_is_untouched() -> None:
    message = AssistantMessage(content="just text, not json")
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message


def test_object_without_protocol_fields_untouched() -> None:
    message = AssistantMessage(content=json.dumps({"foo": "bar"}))
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message


def test_args_not_object_untouched() -> None:
    message = AssistantMessage(
        content=json.dumps({"function": "search", "args": "x"}),
    )
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message


def test_partially_valid_array_is_all_or_nothing() -> None:
    content = json.dumps(
        [
            {"function": "a", "args": {}},
            {"function": "b"},  # без args — ломает всю пачку
        ]
    )
    message = AssistantMessage(content=content)
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message


def test_native_tool_calls_present_skips_fallback() -> None:
    message = AssistantMessage(
        content=json.dumps({"function": "search", "args": {}}),
        tool_calls=(ToolCall(id="c1", name="native", args={}),),
    )
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message


def test_empty_content_untouched() -> None:
    message = AssistantMessage(content="")
    out = ToolCallFromContentFallback().decode(message, model="m")

    assert out is message
