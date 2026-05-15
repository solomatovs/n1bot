"""Pytest-фикстуры пакета boba-agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from boba.agent.orchestrator import AgentContext, AgentRequest
from boba.agent.turn.spec import TurnState
from boba.agent.workspace_fs.shell import FsHistoryWorkspaceShell
from boba.llm.models import Message, ToolResultMessage, new_request_id
from boba.tools.domain import TextResult
from boba.workspace.contract import WorkspaceId


@pytest.fixture
def agent_ctx() -> AgentContext:
    """`AgentContext` с минимальным `AgentRequest` для тестов reducer'ов/middleware."""
    return AgentContext(
        request=AgentRequest(
            request_id=new_request_id(),
            model="test-model",
            query="hi",
        ),
    )


@pytest.fixture
def make_turn_state() -> Callable[..., TurnState]:
    """Фабрика `TurnState` с произвольным набором messages."""

    def _factory(*messages: Message) -> TurnState:
        return TurnState(messages=tuple(messages))

    return _factory


@pytest.fixture
def make_tool_result_message() -> Callable[..., ToolResultMessage]:
    """Фабрика `ToolResultMessage` c `TextResult`-payload по умолчанию."""

    def _factory(call_id: str = "c1", text: str = "ok") -> ToolResultMessage:
        return ToolResultMessage(
            tool_call_id=call_id,
            result=TextResult(text=text),
        )

    return _factory


@pytest.fixture
def history_workspace(tmp_path: Path) -> FsHistoryWorkspaceShell:
    """`FsHistoryWorkspaceShell` с детерминированным `WorkspaceId('test')`."""
    return FsHistoryWorkspaceShell(WorkspaceId("test"), tmp_path)
