"""Инфраструктурный слой: конфиг + DI agent-source + tool-plugins + логирование."""

from boba_next.infra.config import (
    AgentSection,
    AppConfigBootstrap,
    AppCoreConfig,
    AppCoreSection,
)
from boba_next.infra.container import (
    AgentComponents,
    create_agent,
    create_agent_source,
)
from boba_next.infra.dto import LLMConfig, WorkspaceLayout
from boba_next.infra.logging import configure_logging, log_context
from boba_next.infra.tool_plugin_loader import (
    ENTRY_POINTS_GROUP,
    ExtensionContext,
    ToolPluginError,
    ToolPluginLoader,
    ToolPluginLoadError,
    ToolPluginRegisterError,
)

__all__ = [
    "ENTRY_POINTS_GROUP",
    "AgentComponents",
    "AgentSection",
    "AppConfigBootstrap",
    "AppCoreConfig",
    "AppCoreSection",
    "ExtensionContext",
    "LLMConfig",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
    "WorkspaceLayout",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "log_context",
]
