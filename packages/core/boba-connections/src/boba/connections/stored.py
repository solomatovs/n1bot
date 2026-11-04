"""Соединение как сущность: строка хранилища, кому выдано, порт хранилища.

Конкретные типы соединений живут в пакетах-владельцах и попадают сюда через
ConnectionBase; ядро конкретных типов не знает.

Ошибки:
ConnectionStoreError — хранилище отказало или строка не сохранилась.
ConnectionNotFoundError — нет строки с таким id.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.access.grants import GrantTarget
from boba.connections.base import ConnectionBase
from boba.identity.context import Subject

__all__ = [
    "ConnectionNotFoundError",
    "ConnectionRepository",
    "ConnectionStoreError",
    "GrantTarget",
    "GrantedConnection",
    "StoredConnection",
    "StoredRole",
]


class ConnectionStoreError(Exception):
    """База отказала или строка не сохранилась."""


class ConnectionNotFoundError(ConnectionStoreError):
    """В таблице connections нет строки с таким id."""


class StoredRole(BaseModel):
    """Строка roles: имя роли и её id для грантов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str

    @staticmethod
    def by_name(roles: Iterable[StoredRole]) -> Mapping[str, UUID]:
        index: dict[str, UUID] = {}
        for role in roles:
            index[role.name] = role.id

        return index


class StoredConnection(BaseModel):
    """Строка connections с расшифрованным соединением."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    connection: ConnectionBase

    @property
    def kind(self) -> str:
        return self.connection.kind


class GrantedConnection(BaseModel):
    """Строка, выданная субъекту, с признаком дубля имени внутри вида."""

    model_config = ConfigDict(frozen=True)

    row: StoredConnection
    ambiguous: bool


class ConnectionRepository(Protocol):
    """Хранилище соединений и грантов; реализация — ConnectionStore на postgres."""

    @abstractmethod
    async def add(self, name: str, connection: ConnectionBase) -> UUID: ...

    @abstractmethod
    async def update(
        self, connection_id: UUID, name: str, connection: ConnectionBase
    ) -> bool: ...

    @abstractmethod
    async def get(self, connection_id: UUID) -> StoredConnection: ...

    @abstractmethod
    async def list_all(self) -> Sequence[StoredConnection]: ...

    @abstractmethod
    async def remove(self, connection_id: UUID) -> bool: ...

    @abstractmethod
    async def roles(self) -> Sequence[StoredRole]: ...

    @abstractmethod
    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID: ...

    @abstractmethod
    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool: ...

    @abstractmethod
    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]: ...

    @abstractmethod
    async def for_subject(
        self, subject: Subject, kind: str, *, name: str | None = None
    ) -> Sequence[GrantedConnection]: ...
