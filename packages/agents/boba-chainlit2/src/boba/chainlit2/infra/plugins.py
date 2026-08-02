"""Реестр tool-плагинов: секция [tool.<name>] -> langchain-инструменты."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.chainlit2.agent.cancellation import CancellableTools
from boba.chainlit2.agent.tools.access import ToolAccess, ToolAccessGuard
from boba.chainlit2.agent.tools.chart import visualize
from boba.chainlit2.agent.tools.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.chainlit2.agent.tools.confluence.ingest_base import ConfluenceIngestConfig
from boba.chainlit2.agent.tools.confluence.ingest_tools import (
    build_confluence_ingest_tools,
)
from boba.chainlit2.agent.tools.debug import (
    debug_chart,
    debug_error,
    debug_json,
    debug_pg_copy,
    debug_table,
    debug_text,
)
from boba.chainlit2.agent.tools.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.chainlit2.agent.tools.pg import SqlExecutorConfig, build_pg_tools
from boba.chainlit2.agent.tools.sandbox import (
    BashSandboxConfig,
    build_bash_tool,
    has_bwrap,
)
from boba.chainlit2.agent.tools.shell import BashLocalConfig, build_bash_local_tool
from boba.chainlit2.agent.tools.web import WebGrepConfig, build_web_tools
from boba.chainlit2.infra.roles import current_user_roles
from boba.settings import bind
from boba.settings.types import StringList

__all__ = ["PluginMeta", "ToolPlugin", "ToolRegistry", "load_tools"]

logger = logging.getLogger(__name__)

ConfigT = Any


@dataclass(frozen=True)
class ToolPlugin:
    """Один tool-плагин: как собрать инструменты из секции [tool.<name>]."""

    section: str
    build: Callable[[Any], list[BaseTool]]
    config_model: type[BaseModel] | None = None


class PluginMeta(BaseModel):
    """Meta-конфиг плагина: что framework читает из [tool.<name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    roles: StringList = []
    tools: dict[str, StringList] = {}

    def roles_of(self, tool_name: str) -> list[str]:
        return self.tools.get(tool_name) or self.roles


def _build_sandbox_tools(cfg: BashSandboxConfig) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.sandbox] включён, но bubblewrap (bwrap) не найден в PATH — "
            "bash не зарегистрирован",
        )
        return []
    return [build_bash_tool(cfg)]


_PLUGINS: dict[str, ToolPlugin] = {
    "shell": ToolPlugin(
        section="shell",
        config_model=BashLocalConfig,
        build=lambda cfg: [build_bash_local_tool(cfg)],
    ),
    "sandbox": ToolPlugin(
        section="sandbox",
        config_model=BashSandboxConfig,
        build=_build_sandbox_tools,
    ),
    "chart": ToolPlugin(
        section="chart",
        build=lambda _cfg: [visualize],
    ),
    "pg": ToolPlugin(
        section="pg",
        config_model=SqlExecutorConfig,
        build=build_pg_tools,
    ),
    "kb": ToolPlugin(
        section="kb",
        config_model=PostgresKnowledgeBaseConfig,
        build=build_kb_tools,
    ),
    "confluence": ToolPlugin(
        section="confluence",
        config_model=ConfluenceToolsConfig,
        build=build_confluence_tools,
    ),
    "ingest": ToolPlugin(
        section="ingest",
        config_model=ConfluenceIngestConfig,
        build=build_confluence_ingest_tools,
    ),
    "web": ToolPlugin(
        section="web",
        config_model=WebGrepConfig,
        build=build_web_tools,
    ),
    "debug": ToolPlugin(
        section="debug",
        build=lambda _cfg: [
            debug_text,
            debug_json,
            debug_table,
            debug_pg_copy,
            debug_error,
            debug_chart,
        ],
    ),
}


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты и права доступа к ним"""

    tools: list[BaseTool]
    access: ToolAccess

    def for_roles(self, user_roles: Iterable[str]) -> list[BaseTool]:
        roles = frozenset(user_roles)
        allowed = [t for t in self.tools if self.access.allowed(t.name, roles)]
        logger.info(
            "инструментов доступно %d из %d (роли: %s)",
            len(allowed),
            len(self.tools),
            sorted(roles) or "нет",
        )
        return allowed


def load_tools(raw_config: DictConfig) -> ToolRegistry:
    tools: list[BaseTool] = []
    roles_by_tool: dict[str, list[str]] = {}
    for name, plugin in _PLUGINS.items():
        meta = bind(raw_config, f"tool.{name}", PluginMeta)
        if not meta.enable:
            continue
        cfg = (
            bind(raw_config, f"tool.{name}", plugin.config_model)
            if plugin.config_model is not None
            else None
        )
        built = [t for t in plugin.build(cfg) if t.name in meta.tools]
        for tool in built:
            roles_by_tool[tool.name] = meta.roles_of(tool.name)
        tools.extend(built)

    access = ToolAccess(roles_by_tool)
    CancellableTools.guard_all(tools)
    ToolAccessGuard.guard_all(tools, access, current_user_roles)
    return ToolRegistry(tools=tools, access=access)
