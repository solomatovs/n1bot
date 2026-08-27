"""Входы приложения: ссылки на сервисы процесса, резолвятся на каждый вызов."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from boba.connection_broker.user_connections import StoreRef, TicketsRef
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.service import WorkflowService

__all__ = ["RuntimeRefs"]


@dataclass(frozen=True)
class RuntimeRefs:
    """Что приложение отдаёт api и обвязкам инструментов; собирает bootstrap."""

    tool_registry: Callable[[], Awaitable[ToolRegistry]]
    """Реестр инструментов процесса; собирается контейнером на первый запрос."""
    workflow_service: Callable[[], Awaitable[WorkflowService]]
    """Сервис workflow; RuntimeError — секция [workflow] выключена."""
    connection_store: StoreRef
    sso_tickets: TicketsRef
