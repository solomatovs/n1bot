"""boba-tools-v2 — декларативный framework tools на Dishka DI.

Public API:

- `Scope` — `Scope.APP` / `Scope.REQUEST`.
- `FromDI(scope)` — маркер DI-инжектируемого сервиса.
- `FromConfig()` — маркер auto-loaded Pydantic-settings (всегда APP scope).
- `@tool` — пометить class/function как tool для LLM.
- `@provides(scope=...)` — пометить function как service factory для DI.
- `discover_v2_plugins(group)` + `DEFAULT_V2_PLUGIN_GROUP` — entry-point
  discovery v2-плагин-модулей.

Сборка Container'а — задача внешнего слоя (например, `boba.agent.AgentBuilder`).
Этот пакет — чистые декларации и интроспекция; ничего не строит и не запускает.
"""

from __future__ import annotations

from boba.tools_v2.decorators import provides, tool
from boba.tools_v2.discovery import (
    DEFAULT_V2_PLUGIN_GROUP,
    discover_v2_plugins,
)
from boba.tools_v2.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    ToolsV2Error,
)
from boba.tools_v2.markers import FromConfig, FromDI
from boba.tools_v2.scope import Scope

__all__ = [
    "DEFAULT_V2_PLUGIN_GROUP",
    "DuplicateProviderError",
    "FromConfig",
    "FromDI",
    "Scope",
    "ToolDeclarationError",
    "ToolsV2Error",
    "discover_v2_plugins",
    "provides",
    "tool",
]
