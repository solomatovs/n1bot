"""Инфраструктурный слой: загрузка конфигурации, сборка DI-контейнера,
plugin-loader и настройка логирования.

Короткие импорты::

    from boba.infra import AgentComponents, PluginContext, PluginLoader
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
    LLMSamplingSection,
    LLMTransportSection,
    SamplingLoader,
    TomlFileSource,
    TomlSource,
    WorkspacesSection,
    default_config_factory,
    default_resolver,
    load_toml,
)
from boba.infra.container import (
    AgentComponents,
    create_agent,
    create_agent_source,
    create_llm_source,
    create_tools_service,
    default_static_prompt_providers,
)
from boba.infra.logging import configure_logging, log_context
from boba.infra.plugins import (
    PluginContext,
    PluginError,
    PluginLoader,
    PluginLoadError,
    PluginRegisterError,
)

__all__ = [
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
    "LLMSamplingSection",
    "LLMTransportSection",
    "PluginContext",
    "PluginError",
    "PluginLoadError",
    "PluginLoader",
    "PluginRegisterError",
    "SamplingLoader",
    "TomlFileSource",
    "TomlSource",
    "WorkspacesSection",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "create_llm_source",
    "create_tools_service",
    "default_config_factory",
    "default_resolver",
    "default_static_prompt_providers",
    "load_toml",
    "log_context",
]
