"""Pytest-фикстуры пакета boba-agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from boba.agent.agent import AgentContext
from boba.agent.turn.spec import TurnState
from boba.agent.workspace_fs.shell import FsHistoryWorkspaceShell
from boba.llm.models import DialogMessage, ToolResultMessage, new_request_id
from boba.tools.domain import TextResult
from boba.workspace.contract import WorkspaceId


@pytest.fixture
def agent_ctx() -> AgentContext:
    """Минимальный `AgentContext` для тестов reducer'ов/middleware."""
    return AgentContext(
        request_id=new_request_id(),
        query="hi",
    )


@pytest.fixture
def make_turn_state() -> Callable[..., TurnState]:
    """Фабрика `TurnState` с произвольным набором messages."""

    def _factory(*messages: DialogMessage) -> TurnState:
        return TurnState(dialog_messages=tuple(messages))

    return _factory


@pytest.fixture
def make_tool_result_message() -> Callable[..., ToolResultMessage]:
    """Фабрика `ToolResultMessage` c `TextResult`-payload по умолчанию."""

    def _factory(call_id: str = "c1", text: str = "ok") -> ToolResultMessage:
        return ToolResultMessage.from_result(
            tool_call_id=call_id,
            result=TextResult(text=text),
        )

    return _factory


@pytest.fixture
def history_workspace(tmp_path: Path) -> FsHistoryWorkspaceShell:
    """`FsHistoryWorkspaceShell` с детерминированным `WorkspaceId('test')`."""
    return FsHistoryWorkspaceShell(WorkspaceId("test"), tmp_path)
