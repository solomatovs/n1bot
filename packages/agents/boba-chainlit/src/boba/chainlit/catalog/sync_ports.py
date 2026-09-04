"""Порты синхронизации каталога над рантаймом приложения: инструменты
субъекта из реестра процесса и имена подключений из брокера соединений.

Ошибки:
SyncSetupError — подключение субъекту не видно или брокер соединений
    выключен в конфиге.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from boba.catalog_service import (
    ConnectionDirectory,
    ConnectionEntry,
    SyncSetupError,
    SyncTools,
)
from boba.connection_broker.service import UserConnectionsService
from boba.identity.context import Subject
from boba.identity.errors import RefusalError
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.registry import ToolRegistry

__all__ = ["BrokerConnectionDirectory", "RegistrySyncTools"]


class RegistrySyncTools(SyncTools):
    """Реализация SyncTools реестром инструментов процесса: набор вне чата
    по ролям и профилю субъекта."""

    def __init__(self, registry: Callable[[], Awaitable[ToolRegistry]]) -> None:
        self._registry = registry

    async def invoker(self, subject: Subject) -> ToolInvoker:
        registry = await self._registry()
        return ToolInvoker(registry.for_headless(subject.roles, subject.profile))


class BrokerConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory брокером соединений: строки вида глазами
    субъекта для привязки, строка по id для инструмента снятия."""

    def __init__(self, connections: UserConnectionsService) -> None:
        self._connections = connections

    async def visible(self, subject: Subject, kind: str) -> Sequence[ConnectionEntry]:
        try:
            rows = await self._connections.visible(subject, [kind])
        except RuntimeError as exc:
            msg = (
                f"listing {kind} connections for {subject.login!r}: the connection "
                f"broker is unavailable: {exc}"
            )
            raise SyncSetupError(msg) from exc

        entries: list[ConnectionEntry] = []
        for item in rows:
            entries.append(
                ConnectionEntry(
                    id=item.row.id, name=item.row.name, kind=kind, mine=item.mine
                )
            )

        return entries

    async def name_of(self, subject: Subject, connection_id: UUID) -> str:
        try:
            row = await self._connections.visible_row(subject, connection_id)
        except RefusalError as exc:
            msg = f"sync cannot use connection {connection_id}: {exc}"
            raise SyncSetupError(msg) from exc
        except RuntimeError as exc:
            msg = (
                f"sync cannot resolve connection {connection_id}: the connection "
                f"broker is unavailable: {exc}"
            )
            raise SyncSetupError(msg) from exc

        return row.name
