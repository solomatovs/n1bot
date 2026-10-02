"""Входы приложения для стендов: только те сервисы, что стенду нужны, остальные
отказывают ServiceDisabledError при первом обращении — как процесс без секции.

Ошибки:
ServiceDisabledError — стенд попросили сервис, которого у него нет.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.user_connections import StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.identity.errors import ServiceDisabledError
from boba.identity.locks import MemoryLiveLocks
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.messaging.bus import ListenerState, StaticBusWatch
from boba.runtime.refs import RuntimeRefs
from boba.toolrun.registry import ToolRegistry

__all__ = ["StandRefs"]

TicketsRef = Callable[[], SsoTickets | None]


class StandRefs:
    """Сборка RuntimeRefs под стенд: шина, блокировки и слушатель — в памяти."""

    HEARTBEAT_SEC: float = 1.0
    LOCK_TTL_SEC: int = 20
    NAME: str = "stand"

    @classmethod
    def none(cls) -> RuntimeRefs:
        """Ни реестра, ни соединений: как процесс без этих секций."""
        return cls._build(cls._no_registry, cls._disabled_store, cls._no_tickets)

    @classmethod
    def of(cls, store: StoreRef, tickets: TicketsRef) -> RuntimeRefs:
        """Соединения и билеты есть, реестра нет."""
        return cls._build(cls._no_registry, store, tickets)

    @classmethod
    def _build(
        cls,
        tool_registry: Callable[[], Awaitable[ToolRegistry]],
        store: StoreRef,
        tickets: TicketsRef,
    ) -> RuntimeRefs:
        def credentials() -> KerberosCredentialSource:
            return KerberosCredentialSource(tickets(), NoRefresh())

        return RuntimeRefs(
            tool_registry=tool_registry,
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
        msg = (
            f"resolving the tool registry: it is not part of the {StandRefs.NAME} stand"
        )
        raise ServiceDisabledError("tool", msg)

    @staticmethod
    def _no_store() -> ConnectionStore:
        msg = (
            f"resolving the connection store: it is not part of the "
            f"{StandRefs.NAME} stand"
        )
        raise ServiceDisabledError("connections", msg)

    @staticmethod
    def _disabled_store() -> ConnectionStore:
        msg = (
            "resolving the connection store: [connections] is disabled in the "
            "config, user connections are unavailable"
        )
        raise ServiceDisabledError("connections", msg)

    @staticmethod
    def _no_tickets() -> SsoTickets | None:
        return None
