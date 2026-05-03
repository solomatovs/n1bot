"""Инфраструктурный слой: базовые секции, DI agent-source, общие DTO, логирование."""

from boba_next.infra.config import (
    AgentSection,
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

__all__ = [
    "AgentComponents",
    "AgentSection",
    "AppCoreConfig",
    "AppCoreSection",
    "LLMConfig",
    "WorkspaceLayout",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "log_context",
]
