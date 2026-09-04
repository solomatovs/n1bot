"""Порты каталога над рантаймом приложения: инструменты субъекта из реестра
процесса, подключения из брокера соединений и охранник удаления подключения,
которое стоит в источнике каталога.

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
    SyncTools,
)
from boba.connection_broker.service import DeleteGuard, UserConnectionsService
from boba.identity.context import Subject
from boba.identity.errors import RefusalError
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.registry import ToolRegistry

__all__ = ["BoundConnectionGuard", "BrokerConnectionDirectory", "RegistrySyncTools"]


class RegistrySyncTools(SyncTools):
    """Реализация SyncTools реестром инструментов процесса: набор вне чата
    по ролям и профилю субъекта."""

    def __init__(self, registry: Callable[[], Awaitable[ToolRegistry]]) -> None:
        self._registry = registry

    async def invoker(self, subject: Subject) -> ToolInvoker:
        registry = await self._registry()
        return ToolInvoker(registry.for_headless(subject.roles, subject.profile))


class BrokerConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory брокером соединений: строка по id
    глазами субъекта, имя — аргумент инструмента снятия."""

    def __init__(self, connections: UserConnectionsService) -> None:
        self._connections = connections

    async def info_of(self, subject: Subject, connection_id: UUID) -> ConnectionInfo:
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

        return ConnectionInfo(id=row.id, name=row.name, kind=row.kind)


class BoundConnectionGuard(DeleteGuard):
    """Реализация DeleteGuard брокера каталогом: привязанное к источнику
    подключение удалять нельзя, пока его не отвязали. Каталог выключен —
    держать некому."""

    def __init__(self, service: Callable[[], Awaitable[CatalogService]]) -> None:
        self._service = service

    async def holds(self, connection_id: UUID) -> str:
        try:
            service = await self._service()
        except RuntimeError:
            return ""

        holder = await service.sources.holder_of(connection_id)
        if holder is None:
            return ""

        return f"it is bound to catalog source {holder.name!r} ({holder.id})"
