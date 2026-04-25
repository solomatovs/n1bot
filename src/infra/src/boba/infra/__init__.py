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
    build_prompt_providers,
    create_agent,
    create_agent_source,
    create_llm_source,
    create_tools_service,
)
from boba.infra.logging import configure_logging, log_context
from boba.infra.plugins import (
    PluginContext,
    PluginError,
    PluginLoader,
    PluginLoadError,
    PluginRegisterError,
)
from boba.infra.prompts import (
    PromptContext,
    PromptError,
    PromptLoader,
    PromptLoadError,
    PromptRegisterError,
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
    "PromptContext",
    "PromptError",
    "PromptLoadError",
    "PromptLoader",
    "PromptRegisterError",
    "SamplingLoader",
    "TomlFileSource",
    "TomlSource",
    "WorkspacesSection",
    "build_prompt_providers",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "create_llm_source",
    "create_tools_service",
    "default_config_factory",
    "default_resolver",
    "load_toml",
    "log_context",
]
