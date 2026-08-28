"""Таблицы users и threads: строка входа и автор треда без data layer chainlit.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
DataRejectedError — треда нет или у него нет автора.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from boba.chat.threads import (
    DataRejectedError,
    DataUnavailableError,
    ThreadOwnership,
)
from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.session import UserMetadataField
from boba.identity.signin import SignedIn
from boba.runtime.tables import ChatTable, ThreadsColumn, UsersColumn

__all__ = ["UsersTable"]


class UsersTable(PersistedUsers, ThreadOwnership, UsersUpsert):
    """users/threads той же схемы, что пишет data layer чата: чтение и строка входа."""

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        self._postgres = postgres
        self._schema = db_schema
        self._pool_ref = pool

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._postgres)

        return self._pool_ref

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        query = sql.SQL(
            """
            select
                {id},
                {identifier},
                {meta}
            from
                {users}
            where
                {identifier} = %(identifier)s
            limit
                1
            """
        ).format(
            id=UsersColumn.ID.ident(),
            identifier=UsersColumn.IDENTIFIER.ident(),
            meta=UsersColumn.META.ident(),
            users=ChatTable.USERS.under(self._schema),
        )

        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"identifier": identifier})
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError("get_user", str(exc)) from exc

        if row is None:
            return None

        metadata = row[2]
        if metadata is None:
            metadata = {}

        return AuthenticatedUser(id=str(row[0]), identifier=row[1], metadata=metadata)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        """Строка входа: новая либо metadata поверх прежней; билет SSO не пишется."""
        metadata = dict(signed.metadata)
        metadata.pop(UserMetadataField.TICKET, None)
        query = sql.SQL(
            """
            insert into {users} (
                {uuid},
                {identifier},
                {created_at},
                {meta}
            )
            values (
                %(uuid)s,
                %(identifier)s,
                %(created_at)s,
                %(meta)s
            )
            on conflict ({identifier}) do update set
                {meta} = coalesce({users}.{meta}, '{{}}'::jsonb) || excluded.{meta}
            returning
                {id},
                {identifier},
                {meta}
            """
        ).format(
            users=ChatTable.USERS.under(self._schema),
            id=UsersColumn.ID.ident(),
            uuid=UsersColumn.UUID.ident(),
            identifier=UsersColumn.IDENTIFIER.ident(),
            created_at=UsersColumn.CREATED_AT.ident(),
            meta=UsersColumn.META.ident(),
        )
        params = {
            "uuid": uuid4(),
            "identifier": signed.identifier,
            "created_at": datetime.now(UTC),
            "meta": Jsonb(metadata),
        }

        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, params)
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError("ensure_user", str(exc)) from exc

        if row is None:
            raise DataUnavailableError("ensure_user", "users row was not returned")

        return AuthenticatedUser(id=str(row[0]), identifier=row[1], metadata=row[2])

    async def get_thread_author(self, thread_id: str) -> str:
        query = sql.SQL(
            """
            select
                u.{identifier}
            from
                {threads} t
                inner join {users} u on
                    t.{user_id} = u.{id}
            where
                t.{thread_id} = %(id)s
            """
        ).format(
            identifier=UsersColumn.IDENTIFIER.ident(),
            threads=ChatTable.THREADS.under(self._schema),
            users=ChatTable.USERS.under(self._schema),
            user_id=ThreadsColumn.USER_ID.ident(),
            id=UsersColumn.ID.ident(),
            thread_id=ThreadsColumn.ID.ident(),
        )

        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"id": UUID(thread_id)})
                row = await cur.fetchone()
        except ValueError as exc:
            raise DataRejectedError("get_thread_author", str(exc)) from exc
        except Exception as exc:
            raise DataUnavailableError("get_thread_author", str(exc)) from exc

        if row is None:
            raise DataRejectedError(
                "get_thread_author", f"thread {thread_id} not found"
            )

        if row[0] is None:
            raise DataRejectedError(
                "get_thread_author", f"thread {thread_id} has no author"
            )

        return row[0]
