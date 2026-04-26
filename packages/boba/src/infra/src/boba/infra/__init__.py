"""Инфраструктурный слой: framework конфигурации, DI-сборка agent-source,
tool-plugin loader, настройка логирования.

Adapter-зависимый wiring живёт в boba-adapter-*; конкретные ConfigSource —
в boba-config-*. Bootstrap приложения сам собирает цепочку источников
и регистрирует секции в ConfigFactory.
"""

from boba.infra.config import (
    CONFIG_SECTIONS_ENTRY_POINT,
    AgentSection,
    AppCoreConfig,
    AppCoreSection,
    ConfigBundle,
    ConfigError,
    ConfigFactory,
    ConfigLoader,
    ConfigSectionAlreadyRegisteredError,
    ConfigSectionMissingError,
    DefaultSource,
)
from boba.infra.container import (
    AgentComponents,
    create_agent,
    create_agent_source,
)
from boba.infra.logging import configure_logging, log_context
from boba.infra.tool_plugin_loader import (
    ENTRY_POINTS_GROUP,
    ExtensionContext,
    ToolPluginError,
    ToolPluginLoader,
    ToolPluginLoadError,
    ToolPluginRegisterError,
)

__all__ = [
    "CONFIG_SECTIONS_ENTRY_POINT",
    "ENTRY_POINTS_GROUP",
    "AgentComponents",
    "AgentSection",
    "AppCoreConfig",
    "AppCoreSection",
    "ConfigBundle",
    "ConfigError",
    "ConfigFactory",
    "ConfigLoader",
    "ConfigSectionAlreadyRegisteredError",
    "ConfigSectionMissingError",
    "DefaultSource",
    "ExtensionContext",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "log_context",
]
