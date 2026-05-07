"""Tools framework: ToolSource + ToolsService.

Application-слой над `boba.tools.domain` (Tool ABC, ToolResult, etc).
Plugin discovery — отдельная инфра в `boba.plugin.discovery`.
"""

from __future__ import annotations

from boba.tools.framework.registry import (
    StaticToolSource,
    ToolSource,
    ToolsService,
)

__all__ = [
    "StaticToolSource",
    "ToolSource",
    "ToolsService",
]
