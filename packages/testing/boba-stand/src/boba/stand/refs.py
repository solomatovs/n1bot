"""Входы приложения для стендов: только те сервисы, что стенду нужны, остальные
отказывают RuntimeError при первом обращении.

Ошибки:
RuntimeError — стенд попросили сервис, которого у него нет.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.user_connections import StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.identity.locks import MemoryLiveLocks
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.messaging.bus import ListenerState, StaticBusWatch
from boba.runtime.refs import RuntimeRefs
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.service import WorkflowService

__all__ = ["StandRefs"]

TicketsRef = Callable[[], SsoTickets | None]


class StandRefs:
    """Сборка RuntimeRefs под стенд: шина, блокировки и слушатель — в памяти."""

    HEARTBEAT_SEC: float = 1.0
    LOCK_TTL_SEC: int = 20
    NAME: str = "stand"

    @classmethod
    def none(cls) -> RuntimeRefs:
        """Ни реестра, ни workflow, ни соединений: как процесс без этих секций."""
        return cls._build(
            cls._no_registry, cls._no_service, cls._disabled_store, cls._no_tickets
        )

    @classmethod
    def services(
        cls,
        tool_registry: Callable[[], Awaitable[ToolRegistry]],
        workflow_service: Callable[[], Awaitable[WorkflowService]],
    ) -> RuntimeRefs:
        """Реестр и сервис есть, соединений и билетов нет."""
        return cls._build(
            tool_registry, workflow_service, cls._no_store, cls._no_tickets
        )

    @classmethod
    def of(cls, store: StoreRef, tickets: TicketsRef) -> RuntimeRefs:
        """Соединения и билеты есть, реестра и workflow нет."""
        return cls._build(cls._no_registry, cls._no_service, store, tickets)

    @classmethod
    def _build(
        cls,
        tool_registry: Callable[[], Awaitable[ToolRegistry]],
        workflow_service: Callable[[], Awaitable[WorkflowService]],
        store: StoreRef,
        tickets: TicketsRef,
    ) -> RuntimeRefs:
        def credentials() -> KerberosCredentialSource:
            return KerberosCredentialSource(tickets(), NoRefresh())

        return RuntimeRefs(
            tool_registry=tool_registry,
            workflow_service=workflow_service,
            connection_store=store,
            connection_types=ConnectionTypes.discover,
            credentials=credentials,
            live_locks=lambda: MemoryLiveLocks(cls.NAME, cls.LOCK_TTL_SEC),
            heartbeat_sec=cls.HEARTBEAT_SEC,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=lambda: MemoryMessageBus(cls.NAME),
        )

    @staticmethod
    async def _no_registry() -> ToolRegistry:
        msg = "tool registry is not part of this stand"
        raise RuntimeError(msg)

    @staticmethod
    async def _no_service() -> WorkflowService:
        msg = "workflow service is not part of this stand"
        raise RuntimeError(msg)

    @staticmethod
    def _no_store() -> ConnectionStore:
        msg = "connection store is not part of this stand"
        raise RuntimeError(msg)

    @staticmethod
    def _disabled_store() -> ConnectionStore:
        msg = "[connections] is disabled: user connections are unavailable"
        raise RuntimeError(msg)

    @staticmethod
    def _no_tickets() -> SsoTickets | None:
        return None
