"""Соединение как сущность: строка хранилища, кому выдано, порт хранилища.

Конкретные типы профилей живут в пакетах-владельцах и попадают сюда через
ConnectionProfileBase; ядро конкретных типов не знает.

Ошибки:
ConnectionStoreError — хранилище отказало или строка не сохранилась.
ConnectionNotFoundError — нет строки с таким id.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from boba.connections.base import ConnectionProfileBase
from boba.identity.context import Subject

__all__ = [
    "ConnectionNotFoundError",
    "ConnectionRepository",
    "ConnectionStoreError",
    "ConnectionTable",
    "ConnectionsColumn",
    "GrantKind",
    "GrantTarget",
    "GrantsColumn",
    "MissingTypeConnection",
    "RolesColumn",
    "StoredConnection",
    "StoredRole",
    "SubjectConnections",
]


class ConnectionStoreError(Exception):
    """База отказала или строка не сохранилась."""


class ConnectionNotFoundError(ConnectionStoreError):
    """В таблице connections нет строки с таким id."""


class GrantKind(StrEnum):
    """Стороны связи в grants: значение — имя таблицы, kind_id — id в ней."""

    CONNECTIONS = "connections"
    ROLES = "roles"
    USERS = "users"


class ConnectionTable(StrEnum):
    """Таблицы соединений: строки, роли и гранты «источник — субъект»."""

    CONNECTIONS = "connections"
    ROLES = "roles"
    GRANTS = "grants"


class ConnectionsColumn(StrEnum):
    """Колонки connections; профиль лежит шифротекстом в data."""

    ID = "id"
    NAME = "name"
    DATA = "data"


class RolesColumn(StrEnum):
    """Колонки roles."""

    ID = "id"
    ROLE = "role"
    CREATED_AT = "create_at"


class GrantsColumn(StrEnum):
    """Колонки grants: источник (соединение) и субъект (пользователь или роль)."""

    ID = "id"
    SRC_KIND = "src_kind"
    SRC_KIND_ID = "src_kind_id"
    TGT_KIND = "tgt_kind"
    TGT_KIND_ID = "tgt_kind_id"


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


class GrantTarget(BaseModel):
    """Кому выдано соединение: пользователю или роли по id в их таблице."""

    model_config = ConfigDict(frozen=True)

    kind: GrantKind
    id: UUID

    @field_validator("kind")
    @classmethod
    def _target_only(cls, value: GrantKind) -> GrantKind:
        if value is GrantKind.CONNECTIONS:
            msg = (
                f"grant target kind: expected {GrantKind.USERS.value!r} or "
                f"{GrantKind.ROLES.value!r}, got {value.value!r}"
            )
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
    profile: ConnectionProfileBase

    @property
    def kind(self) -> str:
        return self.profile.kind


class MissingTypeConnection(BaseModel):
    """Строка connections, чей тип не установлен: в списках живёт с пометкой."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    kind: str


class SubjectConnections(BaseModel):
    """Соединения субъекта целиком: разобранные строки и строки без типа."""

    model_config = ConfigDict(frozen=True)

    rows: Sequence[StoredConnection] = ()
    missing: Sequence[MissingTypeConnection] = ()


class ConnectionRepository(Protocol):
    """Хранилище соединений и грантов; реализация — ConnectionStore на postgres."""

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def sync_roles(self, names: Iterable[str]) -> None: ...

    @abstractmethod
    async def add(self, name: str, profile: ConnectionProfileBase) -> UUID: ...

    @abstractmethod
    async def add_owned(
        self, name: str, profile: ConnectionProfileBase, user_id: UUID
    ) -> UUID: ...

    @abstractmethod
    async def update(
        self, connection_id: UUID, name: str, profile: ConnectionProfileBase
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
    async def roles(self) -> Sequence[StoredRole]: ...

    @abstractmethod
    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID: ...

    @abstractmethod
    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool: ...

    @abstractmethod
    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]: ...

    @abstractmethod
    async def for_subject(
        self, subject: Subject, kind: str
    ) -> Sequence[StoredConnection]: ...


class ProbeResult(BaseModel):
    """Исход проверки: удалось ли открыть соединение, что ответил сервер, за сколько."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    message: str
    elapsed_ms: int
