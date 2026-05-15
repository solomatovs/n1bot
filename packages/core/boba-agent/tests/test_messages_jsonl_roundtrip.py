"""Round-trip + golden JSONL для Message + JsonLinesMessageService.

Гарантирует:
1. Каждый из 4 Message-вариантов (system/user/assistant/tool_result)
   round-trip'ится через MessageAdapter.
2. Backward-compat: golden JSONL-строки старого формата парсятся.
3. JsonLinesMessageService end-to-end через FsHistoryWorkspaceShell.
4. InMemoryMessageService базовое поведение (add/iter/last/clear).
"""

from __future__ import annotations

from pathlib import Path
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
    MessageAdapter,
    MessageId,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from boba.tools.domain import ErrorResult, JsonResult, TextResult
from boba.workspace.contract import WorkspaceId

_MID = MessageId(UUID("00000000-0000-0000-0000-000000000aaa"))
_TC = ToolCall(id="c1", name="search", args={"q": "hello"})
_ITC = InvalidToolCall(
    id="c2", name="search", raw_args="{bad", error="invalid JSON",
)


def _all_messages() -> list[Any]:
    return [
        SystemMessage(id=_MID, content="be helpful"),
        UserMessage(id=_MID, content="hi"),
        # AssistantMessage без tool_calls
        AssistantMessage(id=_MID, content="hello"),
        # AssistantMessage с tool_calls + invalid_tool_calls
        AssistantMessage(
            id=_MID,
            content="reply",
            tool_calls=(_TC,),
            invalid_tool_calls=(_ITC,),
        ),
        # ToolResultMessage с каждым ToolResult-вариантом
        ToolResultMessage(
            id=_MID,
            tool_call_id="c1",
            result=TextResult(text="ok", metadata={"src": "x"}),
        ),
        ToolResultMessage(
            id=_MID,
            tool_call_id="c1",
            result=JsonResult(payload={"a": [1, 2]}, metadata={}),
        ),
        ToolResultMessage(
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
    a = SystemMessage(content="x")
    b = SystemMessage(content="x")
    assert a.id != b.id
    assert isinstance(a.id, UUID)


def test_assistant_default_empty_tool_calls() -> None:
    a = AssistantMessage(id=_MID, content="x")
    line = MessageAdapter.dump_json(a).decode("utf-8")
    assert '"tool_calls":[]' in line
    assert '"invalid_tool_calls":[]' in line


def test_golden_jsonl_system() -> None:
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa",'
        '"type":"system","content":"be helpful"}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, SystemMessage)
    assert parsed.content == "be helpful"
    assert parsed.id == _MID


def test_golden_jsonl_assistant_with_tools() -> None:
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa",'
        '"type":"assistant","content":"reply",'
        '"tool_calls":[{"id":"c1","name":"search","args":{"q":"x"}}],'
        '"invalid_tool_calls":[]}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, AssistantMessage)
    assert parsed.content == "reply"
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "search"
    assert parsed.tool_calls[0].args == {"q": "x"}


def test_golden_jsonl_tool_result() -> None:
    golden = (
        '{"id":"00000000-0000-0000-0000-000000000aaa",'
        '"type":"tool_result","tool_call_id":"c1",'
        '"result":{"kind":"text","text":"ok","metadata":{}}}'
    )
    parsed = MessageAdapter.validate_json(golden)
    assert isinstance(parsed, ToolResultMessage)
    assert isinstance(parsed.result, TextResult)
    assert parsed.result.text == "ok"


def test_in_memory_message_service() -> None:
    svc = InMemoryMessageService()
    assert svc.last() is None
    svc.add(SystemMessage(content="s"))
    svc.add(UserMessage(content="u"))
    msgs = list(svc.message_iter())
    assert len(msgs) == 2
    assert isinstance(svc.last(), UserMessage)
    svc.clear()
    assert svc.last() is None


def test_jsonlines_message_e2e(tmp_path: Path) -> None:
    workspace = FsHistoryWorkspaceShell(WorkspaceId("test"), tmp_path)
    svc = JsonLinesMessageService(workspace)

    messages = [
        SystemMessage(content="be helpful"),
        UserMessage(content="hi"),
        AssistantMessage(content="hello"),
        ToolResultMessage(
            tool_call_id="c1",
            result=TextResult(text="ok", metadata={}),
        ),
    ]
    for m in messages:
        svc.add(m)

    # Восстановим из файла через свежий instance.
    fresh = JsonLinesMessageService(workspace)
    recovered = list(fresh.message_iter())
    assert recovered == messages


def test_jsonlines_message_clear(tmp_path: Path) -> None:
    workspace = FsHistoryWorkspaceShell(WorkspaceId("test"), tmp_path)
    svc = JsonLinesMessageService(workspace)
    svc.add(SystemMessage(content="x"))
    assert len(list(svc.message_iter())) == 1
    svc.clear()
    assert len(list(svc.message_iter())) == 0
