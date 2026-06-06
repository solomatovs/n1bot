"""Pytest-фикстуры пакета boba-tool-doc."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from boba.settings import (
    ConfigBuilder,
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
)
from boba.tools import ToolBuilder
from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def mock_workspace() -> ProjectWorkspaceShell:
    """Замоканный `ProjectWorkspaceShell` для DI."""
    return MagicMock(spec=ProjectWorkspaceShell)


@pytest.fixture
def make_doc_tool_names(
    mock_workspace: ProjectWorkspaceShell,
) -> Callable[[dict[str, Any]], list[str]]:
    """Фабрика: meta-секция `[tool.doc]` → имена зарегистрированных tool'ов."""

    def _factory(meta_section: dict[str, Any]) -> list[str]:
        config = ConfigBuilder().add_dict({"tool": {"doc": meta_section}}).build()
        tb = (
            ToolBuilder()
            .use_config_resolver(OmegaConfResolver(config))
            .discover_plugins(
                "boba.plugins",
                plugin_tool_filter=OmegaConfPluginToolFilter(config),
            )
            .register_instance(mock_workspace, provides=ProjectWorkspaceShell)
        )
        registry = tb.build()
        try:
            return sorted(t.name for t in tb.tools)
        finally:
            registry.close()

    return _factory
