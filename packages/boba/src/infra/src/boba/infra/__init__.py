"""Инфраструктурный слой: framework конфигурации, DI-сборка agent-source,
tool-plugin loader и настройка логирования.

Adapter-зависимый wiring (LLM-source, file-prompt loader, secp-секции
для адаптеров) живёт в соответствующих boba-adapter-* пакетах —
core ничего про конкретные SDK не знает. Конкретные
ConfigSource-реализации (env, TOML,
…) — в пакетах boba-config-*. Bootstrap приложения сам собирает
свою цепочку источников и регистрирует нужные секции в
ConfigFactory.

Короткие импорты::

    from boba.infra import AgentComponents, ExtensionContext
    from boba.infra import ToolPluginLoader
    from boba.infra import create_agent, create_agent_source
    from boba.infra import (
        ConfigBundle, ConfigFactory, ConfigLoader, configure_logging,
    )
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
