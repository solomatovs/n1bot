"""Инфраструктура web-chainlit: секции конфига, DI agent-loop, логирование."""

from boba.web.chainlit.infra.config import AppCoreConfig
from boba.web.chainlit.infra.container import (
    AgentComponents,
    create_agent,
    create_agent_source,
)
from boba.web.chainlit.infra.logging import configure_logging, log_context

__all__ = [
    "AgentComponents",
    "AppCoreConfig",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "log_context",
]
