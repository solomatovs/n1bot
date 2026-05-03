"""Инфраструктура cli-agent-run: секции конфига, DI agent-loop, логирование."""

from boba.cli.agent_run.infra.config import (
    AgentSection,
    AppCoreConfig,
    AppCoreSection,
)
from boba.cli.agent_run.infra.container import (
    AgentComponents,
    create_agent,
    create_agent_source,
)
from boba.cli.agent_run.infra.logging import configure_logging, log_context

__all__ = [
    "AgentComponents",
    "AgentSection",
    "AppCoreConfig",
    "AppCoreSection",
    "configure_logging",
    "create_agent",
    "create_agent_source",
    "log_context",
]
