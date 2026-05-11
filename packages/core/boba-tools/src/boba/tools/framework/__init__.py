"""Tools framework: ToolSource + ToolExecutor.

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
    ToolExecutor,
    ToolSource,
)

__all__ = [
    "StaticToolSource",
    "ToolDecoratorFactory",
    "ToolExecutor",
    "ToolSource",
    "tool",
    "tool_factory",
]
