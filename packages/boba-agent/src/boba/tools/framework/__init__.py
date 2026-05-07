"""Tools framework: registry + ToolsService.

Application-слой над `boba.tools.domain` (Tool ABC, ToolResult, etc).
Plugin discovery — отдельная инфра в `boba.plugin.discovery`.
"""

from __future__ import annotations

from boba.tools.framework.registry import (
    StaticToolSource,
    ToolCatalog,
    ToolFactory,
    ToolSource,
    ToolsService,
    ToolStore,
)

__all__ = [
    "StaticToolSource",
    "ToolCatalog",
    "ToolFactory",
    "ToolSource",
    "ToolStore",
    "ToolsService",
]
