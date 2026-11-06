"""Параметры connections стенда используют серверный resolver каталога.

Ошибки:
RefusalError — профиль не выдан субъекту, имя неоднозначно или нет credentials.
ConnectionStoreError — хранилище соединений недоступно.
ToolConfigError — некорректное объявление параметра или профиль в таблице.
KerberosError — не удалось подготовить билет вызова.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from boba.access.grants import ConnectionFilter
from boba.connections.base import ConnectionBase
from boba.connections.catalog import CatalogEntry
from boba.connections.credentials import CredentialSource
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import (
    ConnectionNotFoundError,
    ConnectionRepository,
    GrantedConnection,
    GrantTarget,
    StoredConnection,
    StoredRole,
)
from boba.identity.context import CallContexts
from boba.mcp_server.connections.params import ConnectionParams
from boba.mcp_server.connections.user_connections import ArmedConnections
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import StaticConfig

__all__ = ["DisabledConnectionRepository", "StandUserConnections"]


class DisabledConnectionRepository(ConnectionRepository):
    """Репозиторий без данных для тестов, которые не настраивают connections."""

    async def add(self, name: str, connection: ConnectionBase) -> UUID:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def update(
        self, connection_id: UUID, name: str, connection: ConnectionBase
    ) -> bool:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def get(self, connection_id: UUID) -> StoredConnection:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def list_all(self) -> Sequence[StoredConnection]:
        return ()

    async def remove(self, connection_id: UUID) -> bool:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def roles(self) -> Sequence[StoredRole]:
        return ()

    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool:
        raise ConnectionNotFoundError("test connection catalog is disabled")

    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]:
        return ()

    async def for_subject(
        self, subject: object, kind: str, *, name: str | None = None
    ) -> Sequence[GrantedConnection]:
        return ()

    async def catalog(
        self, subject: object, flt: ConnectionFilter
    ) -> Sequence[CatalogEntry]:
        return ()


class StandUserConnections:
    """Собирает ConnectionParams из реального хранилища и credentials стенда."""

    def __init__(
        self,
        store: ConnectionRepository,
        credentials: CredentialSource,
        types: ConnectionTypes,
        contexts: CallContexts,
    ) -> None:
        resolver = ArmedConnections(store, credentials, contexts)
        self._params = ConnectionParams(types, contexts, StaticConfig(), resolver)

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._params.bind_all(tools)

    def params(self) -> ConnectionParams:
        return self._params
