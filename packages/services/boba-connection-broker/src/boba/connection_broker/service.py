"""Соединения субъекта как сервис: видимые строки, свои строки, создание, замена и
удаление. Правила владения живут здесь, а не в маршрутах приложений.

Владение = личный грант: строка, выданная пользователю лично, принадлежит ему —
он её заменяет и удаляет; выданная по роли — общая, только для чтения. Имя
уникально среди видимых субъекту строк: инструменты выбирают соединение по имени.

Ошибки:
RefusalError — kind из ConnectionRefusal: NOT_VISIBLE — строки нет среди видимых
    субъекту; NOT_OWNED — строка общая, правит её только владелец; NAME_TAKEN — имя
    занято среди видимых; IN_USE — строку держит другой компонент, удалять нельзя.
ConnectionStoreError — хранилище недоступно.
RuntimeError — секция [connections] выключена: ссылка на хранилище отказала.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.user_connections import StoreRef
from boba.connections.marks import ConnectionRefusal
from boba.connections.profile import (
    ConnectionProfileBase,
    MissingTypeConnection,
    StoredConnection,
)
from boba.identity.context import Subject
from boba.identity.errors import RefusalError

__all__ = [
    "DeleteGuard",
    "DeletedConnection",
    "UserConnectionsService",
    "VisibleConnection",
    "VisibleConnections",
]


class VisibleConnection(BaseModel):
    """Строка, видимая субъекту, и признак владения ею."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: StoredConnection
    mine: bool


class VisibleMissing(BaseModel):
    """Строка без установленного типа и признак владения ею."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: MissingTypeConnection
    mine: bool


class VisibleConnections(BaseModel):
    """Список субъекта целиком: разобранные строки и строки без типа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: Sequence[VisibleConnection] = ()
    missing: Sequence[VisibleMissing] = ()


class DeletedConnection(BaseModel):
    """Итог удаления: имя строки для оповещения и был ли удалён кто-то."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    deleted: bool


class DeleteGuard(Protocol):
    """Кто ещё держит соединение: каталог не даёт удалить привязанную строку.
    Возвращает причину отказа либо пустую строку."""

    @abstractmethod
    async def holds(self, connection_id: UUID) -> str: ...


class UserConnectionsService:
    """Строки connections глазами субъекта: только свои и выданные по роли.
    Охранники удаления (DeleteGuard) отвечают отказом IN_USE, пока строку
    держит другой компонент."""

    def __init__(self, store_ref: StoreRef, guards: Sequence[DeleteGuard] = ()) -> None:
        self._store_ref = store_ref
        self._guards = tuple(guards)

    async def visible(
        self, subject: Subject, kinds: Sequence[str]
    ) -> list[VisibleConnection]:
        """Свои и выданные по роли строки указанных видов с признаком владения."""
        store = self._store_ref()
        rows = await self._visible_rows(store, subject, kinds)
        owned = await store.owned_ids(subject.user_id)

        visible: list[VisibleConnection] = []
        for row in rows:
            visible.append(VisibleConnection(row=row, mine=row.id in owned))

        return visible

    async def visible_all(self, subject: Subject) -> VisibleConnections:
        """Все видимые строки субъекта; строки без типа — отдельно, с пометкой."""
        store = self._store_ref()
        found = await store.for_subject_all(subject)
        owned = await store.owned_ids(subject.user_id)

        visible: list[VisibleConnection] = []
        for row in found.rows:
            visible.append(VisibleConnection(row=row, mine=row.id in owned))

        broken: list[VisibleMissing] = []
        for lost in found.missing:
            broken.append(VisibleMissing(row=lost, mine=lost.id in owned))

        return VisibleConnections(rows=visible, missing=broken)

    async def visible_row(
        self, subject: Subject, connection_id: UUID
    ) -> StoredConnection:
        """Видимая субъекту строка по id; иначе NOT_VISIBLE."""
        store = self._store_ref()
        rows = (await store.for_subject_all(subject)).rows
        for row in rows:
            if row.id == connection_id:
                return row

        msg = (
            f"connection #{connection_id} not found among the connections "
            f"visible to {subject.login!r}"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    async def create(
        self, subject: Subject, name: str, profile: ConnectionProfileBase
    ) -> StoredConnection:
        """Новая строка с личным грантом субъекту."""
        store = self._store_ref()
        await self._require_free_name(store, subject, name, except_id=None)

        connection_id = await store.add_owned(name, profile, subject.user_id)

        return await store.get(connection_id)

    async def replace(
        self,
        subject: Subject,
        connection_id: UUID,
        name: str,
        profile: ConnectionProfileBase,
    ) -> StoredConnection:
        """Имя и профиль своей строки целиком."""
        store = self._store_ref()
        await self._require_owned(store, subject, connection_id)
        await self._require_free_name(store, subject, name, except_id=connection_id)

        await store.update(connection_id, name, profile)

        return await store.get(connection_id)

    async def delete(self, subject: Subject, connection_id: UUID) -> DeletedConnection:
        """Удаление своей строки; общая или чужая — отказ."""
        store = self._store_ref()
        await self._require_owned(store, subject, connection_id)

        for guard in self._guards:
            reason = await guard.holds(connection_id)
            if reason:
                msg = f"connection #{connection_id} cannot be deleted: {reason}"
                raise RefusalError(ConnectionRefusal.IN_USE, msg)

        # имя читается без разбора профиля: удаляться должна и строка без типа
        name = await store.name_of(connection_id)
        deleted = await store.remove(connection_id)

        return DeletedConnection(name=name, deleted=deleted)

    @staticmethod
    async def _visible_rows(
        store: ConnectionStore, subject: Subject, kinds: Sequence[str]
    ) -> list[StoredConnection]:
        rows: list[StoredConnection] = []
        for kind in kinds:
            rows.extend(await store.for_subject(subject, kind))

        return rows

    async def _require_owned(
        self, store: ConnectionStore, subject: Subject, connection_id: UUID
    ) -> None:
        """NOT_VISIBLE — не видно субъекту, NOT_OWNED — видно, но общее."""
        owned = await store.owned_ids(subject.user_id)
        if connection_id in owned:
            return

        rows = (await store.for_subject_all(subject)).rows
        for row in rows:
            if row.id == connection_id:
                msg = (
                    f"connection #{connection_id} ({row.name!r}) is shared, "
                    f"not owned by {subject.login!r}: only its owner edits it"
                )
                raise RefusalError(ConnectionRefusal.NOT_OWNED, msg)

        msg = (
            f"connection #{connection_id} not found among the connections "
            f"visible to {subject.login!r}"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    async def _require_free_name(
        self,
        store: ConnectionStore,
        subject: Subject,
        name: str,
        except_id: UUID | None,
    ) -> None:
        rows = (await store.for_subject_all(subject)).rows
        for row in rows:
            if row.id == except_id:
                continue

            if row.name == name:
                msg = (
                    f"connection name {name!r} is already used by connection "
                    f"#{row.id} visible to {subject.login!r}"
                )
                raise RefusalError(ConnectionRefusal.NAME_TAKEN, msg)
