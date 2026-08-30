"""Соединение как сущность: рабочий профиль (postgres, clickhouse, web),
его вид, кому выдано, строка хранилища и порт хранилища.

Ошибки:
ConnectionStoreError — хранилище отказало или строка не сохранилась.
ConnectionNotFoundError — нет строки с таким id.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Protocol, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile
from boba.connections.postgres import PostgresConfig
from boba.identity.context import Subject

__all__ = [
    "ConnectionKind",
    "ConnectionNotFoundError",
    "ConnectionProfile",
    "ConnectionRepository",
    "ConnectionStoreError",
    "GrantKind",
    "GrantTarget",
    "StoredConnection",
]


class ConnectionStoreError(Exception):
    """База отказала или строка не сохранилась."""


class ConnectionNotFoundError(ConnectionStoreError):
    """В таблице connections нет строки с таким id."""


ConnectionProfile: TypeAlias = Annotated[
    PostgresConfig | ClickHouseConfig | HttpProfile,
    Field(discriminator="kind"),
]
"""Рабочая модель соединения; jsonb строки разбирается по полю kind."""


class ConnectionKind(StrEnum):
    """Виды соединений: значение — дискриминатор kind рабочей модели."""

    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"
    WEB = "web"

    @classmethod
    def of(cls, profile: ConnectionProfile) -> ConnectionKind:
        return cls(profile.kind)


class GrantKind(StrEnum):
    """Стороны связи в grants: значение — имя таблицы, kind_id — id в ней."""

    CONNECTIONS = "connections"
    ROLES = "roles"
    USERS = "users"


class GrantTarget(BaseModel):
    """Кому выдано соединение: пользователю или роли по id в их таблице."""

    model_config = ConfigDict(frozen=True)

    kind: GrantKind
    id: UUID

    @field_validator("kind")
    @classmethod
    def _target_only(cls, value: GrantKind) -> GrantKind:
        if value is GrantKind.CONNECTIONS:
            msg = "grant target must be a user or a role, not a connection"
            raise ValueError(msg)

        return value

    @classmethod
    def user(cls, user_id: UUID) -> GrantTarget:
        return cls(kind=GrantKind.USERS, id=user_id)

    @classmethod
    def role(cls, role_id: UUID) -> GrantTarget:
        return cls(kind=GrantKind.ROLES, id=role_id)


class StoredConnection(BaseModel):
    """Строка connections с расшифрованным профилем."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    profile: ConnectionProfile

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.of(self.profile)


class ConnectionRepository(Protocol):
    """Хранилище соединений и грантов; реализация — ConnectionStore на postgres."""

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def sync_roles(self, names: Iterable[str]) -> None: ...

    @abstractmethod
    async def add(self, name: str, profile: ConnectionProfile) -> UUID: ...

    @abstractmethod
    async def add_owned(
        self, name: str, profile: ConnectionProfile, user_id: UUID
    ) -> UUID: ...

    @abstractmethod
    async def update(
        self, connection_id: UUID, name: str, profile: ConnectionProfile
    ) -> bool: ...

    @abstractmethod
    async def owned_ids(self, user_id: UUID) -> frozenset[UUID]: ...

    @abstractmethod
    async def get(self, connection_id: UUID) -> StoredConnection: ...

    @abstractmethod
    async def list_all(self) -> Sequence[StoredConnection]: ...

    @abstractmethod
    async def remove(self, connection_id: UUID) -> bool: ...

    @abstractmethod
    async def roles(self) -> dict[str, UUID]: ...

    @abstractmethod
    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID: ...

    @abstractmethod
    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool: ...

    @abstractmethod
    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]: ...

    @abstractmethod
    async def for_subject(
        self, subject: Subject, kind: ConnectionKind
    ) -> Sequence[StoredConnection]: ...
