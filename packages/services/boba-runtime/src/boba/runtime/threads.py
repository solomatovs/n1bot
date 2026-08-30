"""Таблица threads чата: единственный владелец строки треда.

DDL, чтение, upsert с правкой meta по ключам, удаление с владельцем в ответе,
список тредов пользователя и автор треда для проверки владения.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
DataRejectedError — треда нет, у него нет автора либо id не uuid.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from boba.chat.threads import (
    ChatThreads,
    DataRejectedError,
    DataUnavailableError,
    StoredThread,
    ThreadUpsert,
    ThreadUpserted,
)
from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool
from boba.runtime.tables import ChatTable, ThreadsColumn, UsersColumn

__all__ = ["ThreadsTable"]


class ThreadsTable(ChatThreads):
    """threads приложения рядом с users той же схемы."""

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

    def _threads(self) -> sql.Identifier:
        return ChatTable.THREADS.under(self._schema)

    def _row_columns(self) -> sql.Composed:
        return sql.SQL(", ").join(
            [
                ThreadsColumn.ID.ident(),
                ThreadsColumn.CREATED_AT.ident(),
                ThreadsColumn.NAME.ident(),
                ThreadsColumn.USER_ID.ident(),
                ThreadsColumn.TAGS.ident(),
                ThreadsColumn.META.ident(),
            ]
        )

    @staticmethod
    def _stored(row: tuple[Any, ...]) -> StoredThread:
        name = row[2]
        if name is None:
            name = ""

        tags = row[4]
        if tags is None:
            tags = ()

        meta = row[5]
        if meta is None:
            meta = {}

        return StoredThread(
            id=row[0],
            created_at=row[1],
            name=name,
            user_id=row[3],
            tags=tuple(tags),
            meta=meta,
        )

    async def setup(self) -> None:
        """Создаёт таблицу threads и индекс по владельцу; повтор безвреден."""
        ddl = (
            sql.SQL(
                """
                create table if not exists {threads} (
                    {id}         uuid primary key,
                    {created_at} timestamptz not null,
                    {name}       text,
                    {user_id}    uuid,
                    {tags}       text[],
                    {meta}       jsonb
                )
                """
            ).format(
                threads=self._threads(),
                id=ThreadsColumn.ID.ident(),
                created_at=ThreadsColumn.CREATED_AT.ident(),
                name=ThreadsColumn.NAME.ident(),
                user_id=ThreadsColumn.USER_ID.ident(),
                tags=ThreadsColumn.TAGS.ident(),
                meta=ThreadsColumn.META.ident(),
            ),
            sql.SQL(
                """
                create index if not exists idx_threads_user_id
                    on {threads} ({user_id})
                """
            ).format(threads=self._threads(), user_id=ThreadsColumn.USER_ID.ident()),
        )
        try:
            pool = await self._pool()
            async with pool.connection() as conn, conn.transaction():
                for statement in ddl:
                    await conn.execute(statement)
        except Exception as exc:
            raise DataUnavailableError("threads.setup", str(exc)) from exc

    async def get(self, thread_id: UUID) -> StoredThread | None:
        query = sql.SQL(
            "select {cols} from {threads} where {id} = %(id)s"
        ).format(
            cols=self._row_columns(),
            threads=self._threads(),
            id=ThreadsColumn.ID.ident(),
        )
        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"id": thread_id})
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError("get_thread", str(exc)) from exc

        if row is None:
            return None

        return self._stored(row)

    async def upsert(self, change: ThreadUpsert) -> ThreadUpserted:
        query = sql.SQL(
            """
            insert into {threads} as t (
                {id},
                {created_at},
                {name},
                {user_id},
                {tags},
                {meta}
            )
            values (
                %(id)s,
                %(created_at)s,
                %(name)s,
                %(user_id)s,
                %(tags)s,
                %(meta_set)s
            )
            on conflict ({id}) do update set
                {name}    = coalesce(excluded.{name}, t.{name}),
                {user_id} = coalesce(excluded.{user_id}, t.{user_id}),
                {tags}    = coalesce(excluded.{tags}, t.{tags}),
                {meta}    = (coalesce(t.{meta}, '{{}}'::jsonb) - %(meta_del)s::text[])
                            || %(meta_set)s::jsonb
            returning
                {user_id},
                {name},
                (xmax = 0) as inserted
            """
        ).format(
            threads=self._threads(),
            id=ThreadsColumn.ID.ident(),
            created_at=ThreadsColumn.CREATED_AT.ident(),
            name=ThreadsColumn.NAME.ident(),
            user_id=ThreadsColumn.USER_ID.ident(),
            tags=ThreadsColumn.TAGS.ident(),
            meta=ThreadsColumn.META.ident(),
        )

        tags = None
        if change.tags is not None:
            tags = list(change.tags)

        params = {
            "id": change.id,
            "created_at": datetime.now(UTC),
            "name": change.name,
            "user_id": change.user_id,
            "tags": tags,
            "meta_set": Jsonb(dict(change.meta_set)),
            "meta_del": list(change.meta_del),
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
            raise DataUnavailableError("update_thread", str(exc)) from exc

        if row is None:
            raise DataUnavailableError("update_thread", "upsert returned no row")

        name = row[1]
        if name is None:
            name = ""

        return ThreadUpserted(user_id=row[0], name=name, inserted=bool(row[2]))

    async def delete(self, thread_id: UUID) -> UUID | None:
        query = sql.SQL(
            "delete from {threads} where {id} = %(id)s returning {user_id}"
        ).format(
            threads=self._threads(),
            id=ThreadsColumn.ID.ident(),
            user_id=ThreadsColumn.USER_ID.ident(),
        )
        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"id": thread_id})
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError("delete_thread", str(exc)) from exc

        if row is None:
            return None

        return row[0]

    async def list_of(self, user_id: UUID, limit: int) -> Sequence[StoredThread]:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {threads}
            where
                {user_id} = %(user_id)s
            order by
                {created_at} desc
            limit
                %(limit)s
            """
        ).format(
            cols=self._row_columns(),
            threads=self._threads(),
            user_id=ThreadsColumn.USER_ID.ident(),
            created_at=ThreadsColumn.CREATED_AT.ident(),
        )
        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"user_id": user_id, "limit": limit})
                rows = await cur.fetchall()
        except Exception as exc:
            raise DataUnavailableError("list_threads", str(exc)) from exc

        return [self._stored(row) for row in rows]

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
            threads=self._threads(),
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
