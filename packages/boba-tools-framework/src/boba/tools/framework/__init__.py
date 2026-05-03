"""Tools framework: registry, ToolsService, plugin discovery.

Application-слой над `boba.tools.domain` (Tool ABC, ToolResult, etc).
Предоставляет runtime-машинерию: каталог Tool'ов, диспетчер вызовов,
discovery плагинов через entry-points.
"""

from __future__ import annotations

from boba.tools.framework.plugin_loader import (
    ENTRY_POINTS_GROUP,
    ExtensionContext,
    ToolPluginError,
    ToolPluginLoader,
    ToolPluginLoadError,
    ToolPluginRegisterError,
)
from boba.tools.framework.registry import (
    StaticToolSource,
    ToolCatalog,
    ToolFactory,
    ToolSource,
    ToolsService,
    ToolStore,
)

__all__ = [
    "ENTRY_POINTS_GROUP",
    "ExtensionContext",
    "StaticToolSource",
    "ToolCatalog",
    "ToolFactory",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
    "ToolSource",
    "ToolStore",
    "ToolsService",
]
