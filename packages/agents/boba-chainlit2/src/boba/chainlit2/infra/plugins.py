"""Реестр tool-плагинов: секция [tool.<name>] -> langchain-инструменты."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.chainlit2.agent.tools import build_bash_local_tool
from boba.chainlit2.agent.tools.bash import (
    build_bash_tool,
    has_bwrap,
)
from boba.chainlit2.agent.tools.config import BashLocalConfig, BashSandboxConfig
from boba.chainlit2.agent.tools.debug_tools import (
    debug_chart,
    debug_error,
    debug_json,
    debug_pg_copy,
    debug_table,
    debug_text,
)
from boba.chainlit2.agent.tools.visualize import visualize
from boba.settings import bind
from boba.settings.types import StringList

__all__ = ["PluginMeta", "ToolPlugin", "load_tools"]

logger = logging.getLogger(__name__)

ConfigT = Any  # pydantic-модель конфига плагина или None (у плагина нет секции)


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
    tools: StringList | None = None


def _build_sandbox_tools(cfg: BashSandboxConfig) -> list[BaseTool]:
    """bash регистрируется только при наличии bwrap на хосте."""
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


def load_tools(raw_config: DictConfig) -> list[BaseTool]:
    """Инструменты включённых плагинов, отфильтрованные по allowlist."""
    tools: list[BaseTool] = []
    for name, plugin in _PLUGINS.items():
        meta = bind(raw_config, f"tool.{name}", PluginMeta)
        if not meta.enable:
            continue
        cfg = (
            bind(raw_config, f"tool.{name}", plugin.config_model)
            if plugin.config_model is not None
            else None
        )
        built = plugin.build(cfg)
        if meta.tools is not None:
            allow = set(meta.tools)
            built = [t for t in built if t.name in allow]
        tools.extend(built)
    return tools
