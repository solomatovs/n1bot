"""
boba-tools — декларативный framework tools на Dishka DI.
"""

from __future__ import annotations

from boba.tools.builder import ToolBuilder
from boba.tools.config import (
    ConfigResolver,
    PluginFilterAllowAll,
    PluginToolAllowListFilter,
    PluginToolFilter,
)
from boba.tools.decorators import provides, tool
from boba.tools.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    ToolsFrameworkError,
    UnresolvedDependencyError,
)
from boba.tools.markers import FromConfig, FromDI
from boba.tools.scope import Scope

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
