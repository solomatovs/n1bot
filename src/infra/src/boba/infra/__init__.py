"""Инфраструктурный слой: загрузка конфигурации, сборка DI-контейнера,
harness для интеграционных прогонов и настройка логирования.

Короткие импорты::

    from boba.infra import AgentHarness, AgentComponents
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
    create_empty_tools_service,
    create_llm_source,
    default_static_prompt_providers,
)
from boba.infra.harness import AgentHarness, CapturingSink
from boba.infra.logging import configure_logging, log_context

__all__ = [
    "AgentComponents",
    "AgentHarness",
    "AgentSection",
    "AppCoreSection",
    "CapturingSink",
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
    "SamplingLoader",
    "TomlFileSource",
    "TomlSource",
    "WorkspacesSection",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "create_empty_tools_service",
    "create_llm_source",
    "default_config_factory",
    "default_resolver",
    "default_static_prompt_providers",
    "load_toml",
    "log_context",
]
