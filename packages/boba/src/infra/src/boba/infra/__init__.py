"""Инфраструктурный слой: сборка типизированной конфигурации,
DI-контейнер, prompt/tool-loader'ы и настройка логирования.

Конкретные :class:`~boba.domain.core.config.ConfigSource`-реализации
(env, TOML, …) живут в отдельных пакетах ``boba-config-*`` — core инфра
их не подтягивает. Bootstrap приложения сам собирает свою цепочку и
передаёт готовый резолвер в :func:`default_config_factory`.

Короткие импорты::

    from boba.infra import AgentComponents, ExtensionContext
    from boba.infra import PromptLoader, ToolPluginLoader
    from boba.infra import create_agent, create_agent_source, create_llm_source
    from boba.infra import ConfigLoader, ConfigBundle, configure_logging
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
    LLMTransportSection,
    PromptsSection,
    WorkspacesSection,
    default_config_factory,
)
from boba.infra.container import (
    AgentComponents,
    build_prompt_providers,
    create_agent,
    create_agent_source,
    create_llm_source,
)
from boba.infra.logging import configure_logging, log_context
from boba.infra.prompt_loader import PromptLoader, PromptLoadError
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
    "LLMTransportSection",
    "PromptLoadError",
    "PromptLoader",
    "PromptsSection",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
    "WorkspacesSection",
    "build_prompt_providers",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "create_llm_source",
    "default_config_factory",
    "log_context",
]
