"""Pytest-фикстуры пакета boba-tool-files."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from boba.settings import ConfigSource, DictConfigSource, StringList
from boba.tools import ToolBuilder
from boba.workspace.contract import ProjectWorkspaceShell


# Локальные ConfigSource-адаптеры портов boba.tools: прод-версии живут в
# boba.agent.tool_config, но boba-tool-files от boba-agent не зависит, поэтому
# для тестов держим лёгкие копии (тест-инфраструктура).
class _PluginMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList | None = None


class _ConfigSourceResolver:
    def __init__(self, source: ConfigSource) -> None:
        self._source = source

    def resolve(self, cfg_type: type) -> object:
        mc = getattr(cfg_type, "model_config", {})
        section = mc.get("config_path", "") if isinstance(mc, dict) else ""
        path = (
            tuple(s for s in section.split(".") if s)
            if isinstance(section, str)
            else tuple(section)
        )
        return cfg_type.model_validate(self._source.for_path(path))


class _ConfigSourceFilter:
    def __init__(self, source: ConfigSource) -> None:
        self._source = source

    def check_plugin_name(self, plugin_name: str) -> bool:
        return self._meta(plugin_name).enable

    def check_tool(self, plugin_name: str, tool_name: str) -> bool:
        meta = self._meta(plugin_name)
        if not meta.enable:
            return False
        return meta.tools is None or tool_name in meta.tools

    def _meta(self, plugin_name: str) -> _PluginMeta:
        return _PluginMeta.model_validate(self._source.for_path(("tool", plugin_name)))


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
    создаётся `ToolBuilder` с `DictConfigSource` поверх этого dict'а
    и запускается `discover_plugins("boba.plugins")` — entry-points
    discovery с config-gate.
    """

    def _factory(meta_section: dict[str, Any]) -> list[str]:
        source = DictConfigSource({"tool.files": meta_section})
        tb = (
            ToolBuilder()
            .use_config_resolver(_ConfigSourceResolver(source))
            .discover_plugins(
                "boba.plugins",
                plugin_tool_filter=_ConfigSourceFilter(source),
            )
            .register_instance(mock_workspace, provides=ProjectWorkspaceShell)
        )
        registry = tb.build()
        try:
            return sorted(t.name for t in tb.tools)
        finally:
            registry.close()

    return _factory
