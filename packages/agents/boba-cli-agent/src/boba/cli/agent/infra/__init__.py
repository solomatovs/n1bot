"""Инфраструктура cli-agent-run: логирование."""

from boba.cli.agent.infra.logging import configure_logging, log_context

__all__ = [
    "configure_logging",
    "log_context",
]
