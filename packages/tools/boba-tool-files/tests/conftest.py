"""Pytest-фикстуры пакета boba-tool-files (v2)."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

import boba.tool.files as files_module
from boba.agent.builder import AgentBuilder
from boba.workspace.contract import ProjectWorkspaceShell


def _clear_files_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сброс всех BOBA_TOOL__FILES__* и BOBA_CONFIG_PATH."""
    import os

    for key in list(os.environ):
        if key.startswith("BOBA_TOOL__FILES__"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)


@pytest.fixture
def mock_workspace() -> ProjectWorkspaceShell:
    """Замоканный `ProjectWorkspaceShell` для DI."""
    return MagicMock(spec=ProjectWorkspaceShell)


@pytest.fixture
def make_files_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    mock_workspace: ProjectWorkspaceShell,
) -> Callable[[dict[str, str]], list[str]]:
    """Фабрика: env → имена tool'ов, отдаваемых файловым плагином.

    Очищает `BOBA_TOOL__FILES__*` и `BOBA_CONFIG_PATH`, ставит переданный
    env, прогоняет минимальный AgentBuilder.build() и возвращает список
    зарегистрированных tool-имён (т.е. с применением enable_if).
    """

    def _factory(env: dict[str, str]) -> list[str]:
        _clear_files_env(monkeypatch)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        llm = MagicMock()
        turn = MagicMock()
        turn.has_history_view = lambda: True
        turn.has_tool_catalog = lambda: True

        def _provide_ws() -> ProjectWorkspaceShell:
            return mock_workspace

        ab = (
            AgentBuilder()
            .with_llm(llm)
            .use_turn(turn)
            .use_plugin(files_module)
            .register_provider(_provide_ws)
        )
        agent = ab.build()
        try:
            return sorted(t.plan.name for t in ab._tools)
        finally:
            agent.close()

    return _factory
