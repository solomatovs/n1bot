"""Round-trip + golden JSONL для Message + JsonLinesMessageService.

Гарантирует:
1. Каждый из 4 Message-вариантов (system/user/assistant/tool_result)
   round-trip'ится через MessageAdapter.
2. Backward-compat: golden JSONL-строки старого формата парсятся.
3. JsonLinesMessageService end-to-end через FsHistoryWorkspaceShell.
4. InMemoryMessageService базовое поведение (add/iter/last/clear).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from boba.agent import (
    InMemoryMessageService,
    JsonLinesMessageService,
)
from boba.agent.workspace_fs.shell import FsHistoryWorkspaceShell
from boba.llm.models import (
    AssistantMessage,
    InvalidToolCall,
    InvalidToolCallBlock,
    MessageAdapter,
    MessageId,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from boba.tools.domain import ErrorResult, JsonResult, TextResult

_MID = MessageId(UUID("00000000-0000-0000-0000-000000000aaa"))
_TC = ToolCall(id="c1", name="search", args={"q": "hello"})
_ITC = InvalidToolCall(
    id="c2", name="search", raw_args="{bad", error="invalid JSON",
)


def _all_messages() -> list[Any]:
    return [
        SystemMessage.from_text_with_id("be helpful", id=_MID),
        UserMessage.from_text_with_id("hi", id=_MID),
        # AssistantMessage только с текстом
        AssistantMessage.from_text_with_id("hello", id=_MID),
        # AssistantMessage с разнородными блоками: text + tool_call + invalid_tool_call
        AssistantMessage(
            id=_MID,
            blocks=(
                TextBlock(content="reply"),
                ToolCallBlock(call=_TC),
                InvalidToolCallBlock(invalid=_ITC),
            ),
        ),
        # ToolResultMessage: from_result_with_id рендерит ToolResult в blocks
        ToolResultMessage.from_result_with_id(
            id=_MID,
            tool_call_id="c1",
            result=TextResult(text="ok", metadata={"src": "x"}),
        ),
        ToolResultMessage.from_result_with_id(
            id=_MID,
            tool_call_id="c1",
            result=JsonResult(payload={"a": [1, 2]}, metadata={}),
        ),
        ToolResultMessage.from_result_with_id(
            id=_MID,
            tool_call_id="c1",
            result=ErrorResult(message="boom", error_kind="X", metadata={}),
        ),
    ]


@pytest.mark.parametrize("message", _all_messages(), ids=lambda m: type(m).__name__)
def test_message_roundtrip(message: Any) -> None:
    line = MessageAdapter.dump_json(message).decode("utf-8")
    parsed = MessageAdapter.validate_json(line)
    assert parsed == message
    assert type(parsed) is type(message)


def test_message_default_id_factory() -> None:
    """Без явного id Message получает свежий UUID через new_message_id()."""
    a = SystemMessage.from_text("x")
    b = SystemMessage.from_text("x")
    assert a.id != b.id
    assert isinstance(a.id, UUID)


def test_assistant_text_only_serializes_as_single_text_block() -> None:
    """Только текст → один TextBlock в `blocks`, без tool_call/invalid блоков."""
    a = AssistantMessage.from_text_with_id("x", id=_MID)
    line = MessageAdapter.dump_json(a).decode("utf-8")
    assert '"type":"text"' in line
    assert '"content":"x"' in line
    assert '"type":"tool_call"' not in line
    assert '"type":"invalid_tool_call"' not in line


def test_golden_jsonl_system() -> None:
    """Канонический wire-формат: system с одним text-блоком."""
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa","type":"system",'
        '"blocks":[{"type":"text","content":"be helpful"}]}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, SystemMessage)
    assert parsed.content == "be helpful"
    assert parsed.id == _MID


def test_golden_jsonl_assistant_with_tools() -> None:
    """Канонический wire-формат: assistant с text + tool_call блоками."""
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa","type":"assistant",'
        '"blocks":['
        '{"type":"text","content":"reply"},'
        '{"type":"tool_call","call":'
        '{"id":"c1","name":"search","args":{"q":"x"}}}'
        ']}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, AssistantMessage)
    assert parsed.content == "reply"
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "search"
    assert parsed.tool_calls[0].args == {"q": "x"}


def test_golden_jsonl_tool_result() -> None:
    """Канонический wire-формат: tool_result с блоками + is_error."""
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa",'
        '"type":"tool_result","tool_call_id":"c1",'
        '"blocks":[{"type":"text","content":"ok"}],"is_error":false}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, ToolResultMessage)
    assert len(parsed.blocks) == 1
    assert isinstance(parsed.blocks[0], TextBlock)
    assert parsed.blocks[0].content == "ok"
    assert parsed.is_error is False


def test_in_memory_message_service() -> None:
    svc = InMemoryMessageService()
    assert svc.last() is None
    svc.add(SystemMessage.from_text("s"))
    svc.add(UserMessage.from_text("u"))
    msgs = list(svc.message_iter())
    assert len(msgs) == 2
    assert isinstance(svc.last(), UserMessage)
    svc.clear()
    assert svc.last() is None


def test_jsonlines_message_e2e(history_workspace: FsHistoryWorkspaceShell) -> None:
    svc = JsonLinesMessageService(history_workspace)

    messages = [
        SystemMessage.from_text("be helpful"),
        UserMessage.from_text("hi"),
        AssistantMessage.from_text("hello"),
        ToolResultMessage.from_result(
            tool_call_id="c1",
            result=TextResult(text="ok", metadata={}),
        ),
    ]
    for m in messages:
        svc.add(m)

    # Восстановим из файла через свежий instance.
    fresh = JsonLinesMessageService(history_workspace)
    recovered = list(fresh.message_iter())
    assert recovered == messages


def test_jsonlines_message_clear(history_workspace: FsHistoryWorkspaceShell) -> None:
    svc = JsonLinesMessageService(history_workspace)
    svc.add(SystemMessage.from_text("x"))
    assert len(list(svc.message_iter())) == 1
    svc.clear()
    assert len(list(svc.message_iter())) == 0
