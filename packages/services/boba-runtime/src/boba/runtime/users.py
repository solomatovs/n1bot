"""Таблицы users и threads: строка входа и автор треда без data layer chainlit.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
DataRejectedError — треда нет или у него нет автора.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import sql
from psycopg.rows import tuple_row

from boba.chat.threads import (
    DataRejectedError,
    DataUnavailableError,
    ThreadOwnership,
)
from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import AuthenticatedUser, PersistedUsers

__all__ = ["UsersTable"]


class UsersTable(PersistedUsers, ThreadOwnership):
    """Чтение users/threads той же схемы, что пишет data layer чата."""

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
            "select id, identifier, meta from {users} where identifier = %s limit 1"
        ).format(users=sql.Identifier(self._schema, "users"))

        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, (identifier,))
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError("get_user", str(exc)) from exc

        if row is None:
            return None

        metadata = row[2]
        if metadata is None:
            metadata = {}

        return AuthenticatedUser(id=str(row[0]), identifier=row[1], metadata=metadata)

    async def get_thread_author(self, thread_id: str) -> str:
        query = sql.SQL(
            "select u.identifier from {threads} t "
            "inner join {users} u on t.user_id = u.id where t.id = %(id)s"
        ).format(
            threads=sql.Identifier(self._schema, "threads"),
            users=sql.Identifier(self._schema, "users"),
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
