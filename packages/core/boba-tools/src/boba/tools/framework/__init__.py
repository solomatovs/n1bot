"""Tools framework: ToolSource + ToolRegistry + ToolCatalog + ToolExecutor.

Application-слой над `boba.tools.domain` (Tool ABC, ToolResult, etc).
Plugin discovery — отдельная инфра в `boba.plugin.discovery`.
"""

from __future__ import annotations

from boba.tools.framework.decorator import (
    ToolDecoratorFactory,
    tool,
    tool_factory,
)
from boba.tools.framework.registry import (
    StaticToolSource,
    ToolCatalog,
    ToolExecutor,
    ToolRegistry,
    ToolSource,
)

__all__ = [
    "StaticToolSource",
    "ToolCatalog",
    "ToolDecoratorFactory",
    "ToolExecutor",
    "ToolRegistry",
    "ToolSource",
    "tool",
    "tool_factory",
]
