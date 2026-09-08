"""Порты каталога над рантаймом приложения: инструменты субъекта из реестра
процесса, подключения из брокера соединений и охранник удаления подключения,
которое каталог держит версиями снимка или узлами процессов.

Ошибки:
SyncSetupError — подключение субъекту не видно или брокер соединений
    выключен в конфиге.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from boba.catalog_service import (
    CatalogService,
    ConnectionDirectory,
    ConnectionInfo,
    SyncSetupError,
)
from boba.connection_broker.service import DeleteGuard, UserConnectionsService
from boba.connections.profile import StoredConnection
from boba.identity.context import Subject
from boba.identity.errors import RefusalError, ServiceDisabledError

__all__ = ["BrokerConnectionDirectory", "CatalogHoldGuard"]


class BrokerConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory брокером соединений: строка по id или
    имени глазами субъекта."""

    def __init__(self, connections: UserConnectionsService) -> None:
        self._connections = connections

    async def info_of(self, subject: Subject, connection_id: UUID) -> ConnectionInfo:
        return await self._resolve(
            f"connection {connection_id}",
            self._connections.visible_row(subject, connection_id),
        )

    async def named(self, subject: Subject, name: str) -> ConnectionInfo:
        return await self._resolve(
            f"connection {name!r}", self._connections.visible_named(subject, name)
        )

    @staticmethod
    async def _resolve(
        what: str, lookup: Awaitable[StoredConnection]
    ) -> ConnectionInfo:
        """Ошибки:
        SyncSetupError — строка субъекту не видна или брокер недоступен.
        """
        try:
            row = await lookup
        except RefusalError as exc:
            msg = f"sync cannot use {what}: {exc}"
            raise SyncSetupError(msg) from exc
        except ServiceDisabledError as exc:
            msg = (
                f"sync cannot resolve {what}: the connection broker is "
                f"unavailable: {exc}"
            )
            raise SyncSetupError(msg) from exc

        return ConnectionInfo(id=row.id, name=row.name, kind=row.kind)


class CatalogHoldGuard(DeleteGuard):
    """Реализация DeleteGuard брокера каталогом: подключение с версиями
    снимка или узлами процессов удалять нельзя, пока их не убрали. Хост
    ставит охранника только при включённом каталоге."""

    def __init__(self, service: Callable[[], Awaitable[CatalogService]]) -> None:
        self._service = service

    async def holds(self, connection_id: UUID) -> str:
        service = await self._service()

        return await service.holding_reason(connection_id)
