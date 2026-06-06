"""Pytest-фикстуры пакета boba-tool-files."""

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
def make_files_tool_names(
    mock_workspace: ProjectWorkspaceShell,
) -> Callable[[dict[str, Any]], list[str]]:
    """Фабрика: meta-секция → имена tool'ов, зарегистрированных плагином.

    На вход — dict для `[tool.files]` (поля `enable`/`tools`). Внутри
    создаётся `ToolBuilder` с in-memory root поверх этого dict'а
    и запускается `discover_plugins("boba.plugins")` — entry-points
    discovery с config-gate.
    """

    def _factory(meta_section: dict[str, Any]) -> list[str]:
        config = ConfigBuilder().add_dict({"tool": {"files": meta_section}}).build()
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
