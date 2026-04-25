"""Инфраструктурный слой: загрузка конфигурации, сборка DI-контейнера,
extension-loader и настройка логирования.

Короткие импорты::

    from boba.infra import AgentComponents, ExtensionContext, ExtensionLoader
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
    ExtensionsSection,
    LLMTransportSection,
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
from boba.infra.extensions import (
    ExtensionContext,
    ExtensionError,
    ExtensionLoader,
    ExtensionLoadError,
    ExtensionRegisterError,
)
from boba.infra.logging import configure_logging, log_context

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
    "ExtensionContext",
    "ExtensionError",
    "ExtensionLoadError",
    "ExtensionLoader",
    "ExtensionRegisterError",
    "ExtensionsSection",
    "LLMTransportSection",
    "TomlFileSource",
    "TomlSource",
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
