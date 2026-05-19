"""boba-tools-v2 — декларативный framework tools на Dishka DI.

Публичный API:

- `Scope` — `Scope.APP` / `Scope.REQUEST`.
- `FromDI(scope)` — маркер для DI-инжектируемого сервиса.
- `FromConfig(scope)` — маркер для авто-загружаемого Pydantic-settings.
- `@tool` — пометить class/function как tool для LLM.
- `@provides(scope=...)` — пометить function как service factory для DI.
- `AgentBuilder` — собирает Dishka-контейнер из app + plugins, выдаёт ToolRegistry.
"""

from __future__ import annotations

from boba.tools_v2.container import AgentBuilder
from boba.tools_v2.decorators import provides, tool
from boba.tools_v2.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    ToolsV2Error,
)
from boba.tools_v2.markers import FromConfig, FromDI, InjectMarker
from boba.tools_v2.scope import Scope

__all__ = [
    "AgentBuilder",
    "DuplicateProviderError",
    "FromConfig",
    "FromDI",
    "InjectMarker",
    "Scope",
    "ToolDeclarationError",
    "ToolsV2Error",
    "provides",
    "tool",
]
