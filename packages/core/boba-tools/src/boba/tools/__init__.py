"""boba-tools — декларативный framework tools на Dishka DI.

Три слоя:
- `boba.tools.domain` — контракт (Tool ABC, ToolResult, ToolSchema, identity);
- `boba.tools.framework` — application (ToolRegistry/Catalog/Executor/Source);
- `boba.tools.declarative` — `@tool`/`@provides` + сборка DI в `ToolRegistry`.

Этот модуль-фасад поднимает наверх только декларативный слой — то, что
нужно автору плагина и composition-root'у агента. Контракт и application-слой
импортируются из своих подпакетов напрямую.
"""

from __future__ import annotations

from boba.tools.declarative import (
    ConfigResolver,
    DuplicateProviderError,
    FromConfig,
    FromDI,
    PluginFilterAllowAll,
    PluginToolAllowListFilter,
    PluginToolFilter,
    Scope,
    ToolBuilder,
    ToolDeclarationError,
    ToolsFrameworkError,
    UnresolvedDependencyError,
    provides,
    tool,
)

__all__ = [
    "ConfigResolver",
    "DuplicateProviderError",
    "FromConfig",
    "FromDI",
    "PluginFilterAllowAll",
    "PluginToolAllowListFilter",
    "PluginToolFilter",
    "Scope",
    "ToolBuilder",
    "ToolDeclarationError",
    "ToolsFrameworkError",
    "UnresolvedDependencyError",
    "provides",
    "tool",
]
