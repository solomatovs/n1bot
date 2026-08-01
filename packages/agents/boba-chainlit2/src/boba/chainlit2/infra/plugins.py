"""Реестр tool-плагинов и загрузчик инструментов для langchain-агента.

Каждый плагин — секция [tool.<name>] в конфиге: PluginMeta (enable/tools)
читает framework, остальные поля биндятся в config_model плагина. Загрузчик
строит langchain-инструменты и фильтрует по allowlist tools.

Формат секций совместим с v1 (boba-chainlit): enable + tools + поля конфига.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.chainlit2.agent.tools import build_bash_local_tool
from boba.chainlit2.agent.tools.config import BashLocalConfig
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


_PLUGINS: dict[str, ToolPlugin] = {
    "shell": ToolPlugin(
        section="shell",
        config_model=BashLocalConfig,
        build=lambda cfg: [build_bash_local_tool(cfg)],
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
    """Собрать включённые инструменты из конфига, с allowlist-фильтром.

    Плагин без секции или с enable=false — пропускается. Allowlist tools
    (если задан) оставляет только перечисленные по wire-имени (t.name).
    """
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
