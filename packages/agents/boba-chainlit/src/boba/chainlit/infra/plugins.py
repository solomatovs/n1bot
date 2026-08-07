"""Реестр tool-плагинов: секция [tool.<name>] -> langchain-инструменты."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.chainlit.agent.tools.access import ToolAccess, ToolAccessGuard
from boba.chainlit.agent.tools.cancellation import CancellableTools
from boba.chainlit.agent.tools.errors import ToolErrorGuard
from boba.chainlit.agent.tools.run_log import ToolRunLogger
from boba.chainlit.infra.session import (
    current_thread_id,
    current_user_id,
    current_user_roles,
)
from boba.sandbox import SandboxCaller, SandboxToolConfig, has_bwrap
from boba.settings import bind
from boba.tool.ch import ChExecutorConfig, build_ch_tools
from boba.tool.chart import build_chart_tools
from boba.tool.doc import DocToolsConfig, build_doc_tools
from boba.tool.kb import (
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.tool.kb.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_tools import (
    build_confluence_ingest_tools,
)
from boba.tool.pg import PgExecutorConfig, build_pg_tools
from boba.tool.shell import build_bash_tool
from boba.tool.web import WebGrepConfig, build_web_tools
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.types import StringList

__all__ = ["PluginMeta", "ToolPlugin", "ToolRegistry", "load_tools"]

logger = logging.getLogger(__name__)

ConfigT = Any


@dataclass(frozen=True)
class ToolPlugin:
    """Один tool-плагин: как собрать инструменты из секции [tool.<name>]."""

    section: str
    build: Callable[[Any, LauncherFactory], list[BaseTool]]
    config_model: type[BaseModel] | None = None


class PluginMeta(BaseModel):
    """Meta-конфиг плагина: что framework читает из [tool.<name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    roles: StringList = []
    tools: dict[str, StringList] = {}

    def roles_of(self, tool_name: str) -> list[str]:
        return self.tools.get(tool_name) or self.roles


def _build_sandbox_tools(
    cfg: None,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.bash] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "bash was not registered",
        )
        return []
    return [build_bash_tool(launchers)]


def _build_doc_tools(
    cfg: DocToolsConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.doc] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "doc tools were not registered",
        )
        return []
    return build_doc_tools(cfg, launchers)


def _build_ingest_tools(
    cfg: ConfluenceIngestConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.ingest] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "confluence ingest tools were not registered",
        )
        return []
    return build_confluence_ingest_tools(cfg, launchers)


def _build_confluence_tools(
    cfg: ConfluenceToolsConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.confluence] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "confluence tools were not registered",
        )
        return []
    return build_confluence_tools(cfg, launchers)


def _build_web_tools(
    cfg: WebGrepConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.web] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "web tools were not registered",
        )
        return []
    return build_web_tools(cfg, launchers)


def _build_chart_tools(
    cfg: None,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.chart] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "chart tools were not registered",
        )
        return []
    return build_chart_tools(launchers)


def _build_pg_tools(
    cfg: PgExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.pg] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "pg tools were not registered",
        )
        return []
    return build_pg_tools(cfg, launchers)


def _build_ch_tools(
    cfg: ChExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.ch] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "clickhouse tools were not registered",
        )
        return []
    return build_ch_tools(cfg, launchers)


def _build_kb_tools(
    cfg: PostgresKnowledgeBaseConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    if not has_bwrap():
        logger.warning(
            "[tool.kb] is enabled, but bubblewrap (bwrap) is not in PATH — "
            "kb tools were not registered",
        )
        return []
    return build_kb_tools(cfg, launchers)


def _sandbox_path_vars() -> dict[str, str]:
    """Значения {user_id}/{thread_id} для путей профиля на момент вызова."""
    values = {"user_id": current_user_id(), "thread_id": current_thread_id()}
    return {name: str(value) for name, value in values.items() if value}


def _launchers(sandbox: SandboxToolConfig) -> LauncherFactory:
    """Фабрика исполнителей на профиле инструмента: окружение выбирает приложение."""
    profile = sandbox.effective()

    def launcher(tool: str) -> ToolLauncher:
        return SandboxCaller(tool, profile, _sandbox_path_vars)

    return launcher


_PLUGINS: dict[str, ToolPlugin] = {
    "bash": ToolPlugin(
        section="bash",
        build=_build_sandbox_tools,
    ),
    "doc": ToolPlugin(
        section="doc",
        config_model=DocToolsConfig,
        build=_build_doc_tools,
    ),
    "chart": ToolPlugin(
        section="chart",
        build=_build_chart_tools,
    ),
    "pg": ToolPlugin(
        section="pg",
        config_model=PgExecutorConfig,
        build=_build_pg_tools,
    ),
    "ch": ToolPlugin(
        section="ch",
        config_model=ChExecutorConfig,
        build=_build_ch_tools,
    ),
    "kb": ToolPlugin(
        section="kb",
        config_model=PostgresKnowledgeBaseConfig,
        build=_build_kb_tools,
    ),
    "confluence": ToolPlugin(
        section="confluence",
        config_model=ConfluenceToolsConfig,
        build=_build_confluence_tools,
    ),
    "ingest": ToolPlugin(
        section="ingest",
        config_model=ConfluenceIngestConfig,
        build=_build_ingest_tools,
    ),
    "web": ToolPlugin(
        section="web",
        config_model=WebGrepConfig,
        build=_build_web_tools,
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
            "tools available: %d of %d (roles: %s)",
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
        sandbox = bind(raw_config, f"tool.{name}.sandbox", SandboxToolConfig)
        built = [
            t for t in plugin.build(cfg, _launchers(sandbox)) if t.name in meta.tools
        ]
        for tool in built:
            roles_by_tool[tool.name] = meta.roles_of(tool.name)
        tools.extend(built)

    access = ToolAccess(roles_by_tool)
    ToolRunLogger.guard_all(tools)
    CancellableTools.guard_all(tools)
    ToolAccessGuard.guard_all(tools, access, current_user_roles)
    ToolErrorGuard.guard_all(tools)
    return ToolRegistry(tools=tools, access=access)
