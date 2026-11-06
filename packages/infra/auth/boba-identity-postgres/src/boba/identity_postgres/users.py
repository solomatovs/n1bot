"""Пользователи и роли: только подтверждённые канонические логины.

Пустое чтение возвращает None. Конфликты данных — IdentityConflictError,
отказ PostgreSQL — ошибка доступности, дефекты схемы/строк — внутренняя ошибка.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from boba.db.postgres import Cursor
from boba.db.postgres.errors import PostgresConcurrentChangeError
from boba.identity.api import (
    RoleCatalog,
    StoredUser,
    UserRows,
)
from boba.identity.principal import UserIdentity
from boba.identity_postgres.table import IdentityTable

__all__ = ["UsersTable"]


class UsersColumn(StrEnum):
    ID = "id"
    CREATED_AT = "created_at"
    LOGIN = "login"
    ROLES = "roles"


class UsersTable(IdentityTable, UserRows, RoleCatalog):
    """UUID, подтверждённая личность и членство; сессионных данных здесь нет."""

    SELECT = """
        select
            u.id,
            u.created_at,
            u.login,
            array(
                select
                    r.role
                from
                    {schema}.user_roles as m
                    join {schema}.roles as r on r.id = m.role_id
                where
                    m.user_id = u.id
            ) as roles
        from
            {schema}.users as u
    """

    def _stored(self, row: Mapping[str, Any]) -> StoredUser:
        try:
            return StoredUser(
                id=row[UsersColumn.ID],
                created_at=row[UsersColumn.CREATED_AT],
                identity=UserIdentity(
                    login=row[UsersColumn.LOGIN],
                ),
                roles=frozenset(row[UsersColumn.ROLES]),
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise self._broken("read user", exc) from exc

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        query = (
            self._query()
            .add(self.SELECT)
            .add("where u.login = %(login)s", login=identity.login)
            .build()
        )
        row = await self._row(query, "get user")
        return self._stored(row) if row is not None else None

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        query = (
            self._query()
            .add(self.SELECT)
            .add("where u.id = %(user_id)s", user_id=user_id)
            .build()
        )
        row = await self._row(query, "get user by id")
        return self._stored(row) if row is not None else None

    async def upsert(
        self,
        identity: UserIdentity,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        """Получить/создать UUID и обновить автоматические роли одной транзакцией.

        None сохраняет членство; пустой набор снимает автоматические роли.
        Ручное членство не изменяется. Неизвестные каталогу роли игнорируются.
        """

        async def persist(cur: Cursor) -> StoredUser:
            insert = (
                self._query()
                .add(
                    """
                    insert into {schema}.users (
                        login
                    )
                    values (
                        %(login)s
                    )
                    on conflict (login)
                    do nothing
                    """,
                    login=identity.login,
                )
                .build()
            )
            await cur.execute(insert.text, insert.params)
            # Отдельная команда видит завершившуюся конкурентную вставку при
            # READ COMMITTED. Замок сериализует синхронизацию ролей пользователя.
            select = (
                self._query()
                .add(
                    """
                    select
                        id
                    from
                        {schema}.users
                    where
                        login = %(login)s
                    for update
                    """,
                    login=identity.login,
                )
                .build()
            )
            await cur.execute(select.text, select.params)
            row = await cur.fetchone()
            if row is None:
                raise PostgresConcurrentChangeError(
                    f"user {identity.identifier!r} was removed or renamed "
                    "during sign-in"
                )
            user_id = row[UsersColumn.ID]
            if roles is not None:
                await self._sync_roles(cur, user_id, roles)
            query = (
                self._query()
                .add(self.SELECT)
                .add(
                    """
                    where
                        u.id = %(user_id)s
                    """,
                    user_id=user_id,
                )
                .build()
            )
            await cur.execute(query.text, query.params)
            row = await cur.fetchone()
            if row is None:
                raise self._broken(
                    "read saved user", RuntimeError("locked user is missing")
                )
            return self._stored(row)

        return await self._transacted("ensure user and role membership", persist)

    async def _sync_roles(
        self, cur: Cursor, user_id: UUID, roles: frozenset[str]
    ) -> None:
        delete = (
            self._query()
            .add(
                """
                delete from
                    {schema}.user_roles as m
                using
                    {schema}.roles as r
                where 1=1
                    and m.user_id = %(user_id)s
                    and m.role_id = r.id
                    and m.managed
                    and not (r.role = any(%(roles)s::text[]))
                """,
                user_id=user_id,
                roles=list(roles),
            )
            .build()
        )
        await cur.execute(delete.text, delete.params)
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.user_roles (
                    user_id,
                    role_id,
                    managed
                )
                select
                    %(user_id)s,
                    id,
                    true
                from
                    {schema}.roles
                where
                    role = any(%(roles)s::text[])
                on conflict (user_id, role_id)
                do nothing
                """,
                user_id=user_id,
                roles=list(roles),
            )
            .build()
        )
        await cur.execute(insert.text, insert.params)

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]:
        query = (
            self._query()
            .add(
                """
                select
                    role
                from
                    {schema}.roles
                where
                    role = any(%(roles)s::text[])
                union
                select
                    r.role
                from
                    {schema}.users as u
                    join {schema}.user_roles as m on m.user_id = u.id
                    join {schema}.roles as r on r.id = m.role_id
                where 1=1
                    and u.login = %(login)s
                    and not m.managed
                """,
                roles=list(candidates),
                login=identity.login,
            )
            .build()
        )
        rows = await self._rows(query, "resolve roles")
        return frozenset(row["role"] for row in rows)
