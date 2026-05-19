"""boba-tools — декларативный framework tools на Dishka DI.

Public API:

- `Scope` — `Scope.APP` / `Scope.REQUEST`.
- `FromDI(scope)` — маркер DI-инжектируемого сервиса.
- `FromConfig()` — маркер auto-loaded Pydantic-settings (всегда APP scope).
- `@tool` — пометить class/function как tool для LLM.
- `@provides(scope=...)` — пометить function как service factory для DI.
- `discover_plugins(group)` + `DEFAULT_PLUGIN_GROUP` — entry-point
  discovery плагин-модулей.

Сборка Container'а — задача внешнего слоя (например, `boba.agent.AgentBuilder`).
Этот пакет — чистые декларации и интроспекция; ничего не строит и не запускает.

Runtime-сущности (`Tool`, `ToolRegistry`, `ToolCatalog`, `ToolExecutor`)
живут в `boba.tools.domain` / `boba.tools.framework`.
"""

from __future__ import annotations

from boba.tools.decorators import provides, tool
from boba.tools.discovery import DEFAULT_PLUGIN_GROUP, discover_plugins
from boba.tools.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    ToolsFrameworkError,
)
from boba.tools.markers import FromConfig, FromDI
from boba.tools.scope import Scope

__all__ = [
    "DEFAULT_PLUGIN_GROUP",
    "DuplicateProviderError",
    "FromConfig",
    "FromDI",
    "Scope",
    "ToolDeclarationError",
    "ToolsFrameworkError",
    "discover_plugins",
    "provides",
    "tool",
]
