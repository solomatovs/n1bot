"""Инфраструктурный слой: загрузка конфигурации, сборка DI-контейнера,
prompt/tool-loader'ы и настройка логирования.

Короткие импорты::

    from boba.infra import AgentComponents, ExtensionContext
    from boba.infra import PromptLoader, ToolPluginLoader
    from boba.infra import create_agent, create_agent_source, create_llm_source
    from boba.infra import ConfigLoader, ConfigBundle, configure_logging
"""

from boba.infra.config import (
    AgentSection,
    AppCoreSection,
    ConfigBundle,
    ConfigFactory,
    ConfigLoader,
    ConfigSectionBuilder,
    ConfigState,
    DefaultSource,
    EnvFileSource,
    EnvSource,
    ExtensionsBagSection,
    ExtensionsBagSource,
    LLMTransportSection,
    PromptsSection,
    TomlFileSource,
    TomlSource,
    WorkspacesSection,
    default_config_factory,
    default_resolver,
    load_toml,
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
    "ENTRY_POINTS_GROUP",
    "AgentComponents",
    "AgentSection",
    "AppCoreSection",
    "ConfigBundle",
    "ConfigFactory",
    "ConfigLoader",
    "ConfigSectionBuilder",
    "ConfigState",
    "DefaultSource",
    "EnvFileSource",
    "EnvSource",
    "ExtensionContext",
    "ExtensionsBagSection",
    "ExtensionsBagSource",
    "LLMTransportSection",
    "PromptLoadError",
    "PromptLoader",
    "PromptsSection",
    "TomlFileSource",
    "TomlSource",
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
    "default_resolver",
    "load_toml",
    "log_context",
]
