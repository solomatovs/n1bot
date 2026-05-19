"""Pytest-фикстуры пакета boba-tool-html."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from boba.agent.builder import AgentBuilder
from boba.settings import DictConfigSource
from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def mock_workspace() -> ProjectWorkspaceShell:
    return MagicMock(spec=ProjectWorkspaceShell)


@pytest.fixture
def make_html_tool_names(
    mock_workspace: ProjectWorkspaceShell,
) -> Callable[[dict[str, Any]], list[str]]:
    """Фабрика: meta-секция `[tool.html]` → имена зарегистрированных tool'ов.

    Идёт через `discover_plugins("boba.plugins")` + `DictConfigSource`,
    без monkeypatch'инга env.
    """

    def _factory(meta_section: dict[str, Any]) -> list[str]:
        llm = MagicMock()
        turn = MagicMock()
        turn.has_history_view = lambda: True
        turn.has_tool_catalog = lambda: True

        def _provide_ws() -> ProjectWorkspaceShell:
            return mock_workspace

        source = DictConfigSource({"tool.html": meta_section})
        ab = (
            AgentBuilder()
            .use_llm(llm)
            .use_turn(turn)
            .use_config(source)
            .discover_plugins(entry_point="boba.plugins")
            .register_provider(_provide_ws)
        )
        agent = ab.build()
        try:
            return sorted(t.plan.name for t in ab._tools)
        finally:
            agent.close()

    return _factory
