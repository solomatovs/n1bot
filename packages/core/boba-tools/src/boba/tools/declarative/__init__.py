"""Декларативный слой: @tool/@provides + сборка Dishka-DI в ToolRegistry.

Author-API (пишут в плагинах): tool, provides, FromDI, FromConfig,
Scope. Composition-API (собирают агента): ToolBuilder, ConfigResolver,
PluginToolFilter и его реализации. Ошибки декларации/регистрации — здесь же.

Слой зависит от boba.tools.domain (контракт) и boba.tools.framework
(registry), а также от Dishka. Domain/framework на него не ссылаются.
"""

from __future__ import annotations

from boba.tools.declarative.builder import ToolBuilder
from boba.tools.declarative.config import (
    ConfigResolver,
    PluginFilterAllowAll,
    PluginToolAllowListFilter,
    PluginToolFilter,
)
from boba.tools.declarative.decorators import provides, tool
from boba.tools.declarative.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    ToolsFrameworkError,
    UnresolvedDependencyError,
)
from boba.tools.declarative.inject import FromConfig, FromDI
from boba.tools.declarative.scope import Scope

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
