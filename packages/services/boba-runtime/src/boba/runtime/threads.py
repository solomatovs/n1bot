"""Таблица threads чата: единственный владелец строки треда.

DDL, чтение, upsert с правкой meta по ключам, удаление с владельцем в ответе,
список тредов пользователя и автор треда для проверки владения.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
DataRejectedError — треда нет, у него нет автора либо id не uuid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from boba.chat.threads import (
    ChatThreads,
    DataRejectedError,
    StoredThread,
    ThreadsColumn,
    ThreadUpsert,
    ThreadUpserted,
)
from boba.runtime.table import PgTable

__all__ = ["ThreadsTable"]


class ThreadsTable(PgTable, ChatThreads):
    """threads приложения рядом с users той же схемы."""

    def _stored(self, row: Mapping[str, Any]) -> StoredThread:
        name = row[ThreadsColumn.NAME.value]
        if name is None:
            name = ""

        tags = row[ThreadsColumn.TAGS.value]
        if tags is None:
            tags = ()

        meta = row[ThreadsColumn.META.value]
        if meta is None:
            meta = {}

        return StoredThread(
            id=row[ThreadsColumn.ID.value],
            created_at=row[ThreadsColumn.CREATED_AT.value],
            name=name,
            user_id=row[ThreadsColumn.USER_ID.value],
            tags=tuple(tags),
            meta=meta,
        )

    async def setup(self) -> None:
        """Создаёт таблицу threads и индекс по владельцу; повтор безвреден."""
        ddl = (
            self._query()
            .add(
                """
                create table if not exists {schema}.threads (
                    id         uuid primary key,
                    created_at timestamptz not null,
                    name       text,
                    user_id    uuid,
                    tags       text[],
                    meta       jsonb
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_threads_user_id
                    on {schema}.threads (user_id)
                """
            )
            .build(),
        )

        await self._apply_ddl(ddl)

    async def get(self, thread_id: UUID) -> StoredThread | None:
        query = (
            self._query()
            .add(
                """
                select id, created_at, name, user_id, tags, meta
                from {schema}.threads
                where id = %(id)s
                """,
                id=thread_id,
            )
            .build()
        )
        row = await self._row(query, "get_thread")
        if row is None:
            return None

        return self._stored(row)

    async def upsert(self, change: ThreadUpsert) -> ThreadUpserted:
        tags = None
        if change.tags is not None:
            tags = list(change.tags)

        query = (
            self._query()
            .add(
                """
                insert into {schema}.threads as t (
                    id,
                    created_at,
                    name,
                    user_id,
                    tags,
                    meta
                )
                values (
                    %(id)s,
                    %(created_at)s,
                    %(name)s,
                    %(user_id)s,
                    %(tags)s,
                    %(meta_set)s
                )
                on conflict (id) do update set
                    name    = coalesce(excluded.name, t.name),
                    user_id = coalesce(excluded.user_id, t.user_id),
                    tags    = coalesce(excluded.tags, t.tags),
                    meta    = (coalesce(t.meta, '{{}}'::jsonb) - %(meta_del)s::text[])
                              || %(meta_set)s::jsonb
                returning
                    user_id,
                    name,
                    (xmax = 0) as inserted
                """,
                id=change.id,
                created_at=datetime.now(UTC),
                name=change.name,
                user_id=change.user_id,
                tags=tags,
                meta_set=Jsonb(dict(change.meta_set)),
                meta_del=list(change.meta_del),
            )
            .build()
        )
        row = self._returning(
            await self._row(query, "update_thread"), f"upsert of thread {change.id}"
        )

        name = row[ThreadsColumn.NAME.value]
        if name is None:
            name = ""

        return ThreadUpserted(
            user_id=row[ThreadsColumn.USER_ID.value],
            name=name,
            inserted=bool(row["inserted"]),
        )

    async def delete(self, thread_id: UUID) -> UUID | None:
        query = (
            self._query()
            .add(
                "delete from {schema}.threads where id = %(id)s returning user_id",
                id=thread_id,
            )
            .build()
        )
        row = await self._row(query, "delete_thread")
        if row is None:
            return None

        return row[ThreadsColumn.USER_ID.value]

    async def list_of(self, user_id: UUID, limit: int) -> Sequence[StoredThread]:
        query = (
            self._query()
            .add(
                """
                select
                    id, created_at, name, user_id, tags, meta
                from
                    {schema}.threads
                where
                    user_id = %(user_id)s
                order by
                    created_at desc
                limit
                    %(limit)s
                """,
                user_id=user_id,
                limit=limit,
            )
            .build()
        )
        rows = await self._rows(query, "list_threads")

        threads: list[StoredThread] = []
        for row in rows:
            threads.append(self._stored(row))

        return threads

    async def get_thread_author(self, thread_id: str) -> str:
        try:
            key = UUID(thread_id)
        except ValueError as exc:
            detail = f"thread id must be a uuid, got {thread_id!r}: {exc}"
            raise DataRejectedError("get_thread_author", detail) from exc

        query = (
            self._query()
            .add(
                """
                select
                    u.identifier
                from
                    {schema}.threads t
                    inner join {schema}.users u on
                        t.user_id = u.id
                where
                    t.id = %(id)s
                """,
                id=key,
            )
            .build()
        )
        row = await self._row(query, "get_thread_author")

        if row is None:
            detail = f"thread {thread_id} not found in {self.schema}.threads"
            raise DataRejectedError("get_thread_author", detail)

        identifier = row["identifier"]
        if identifier is None:
            detail = (
                f"thread {thread_id} in {self.schema}.threads has no users row "
                "joined by user_id"
            )
            raise DataRejectedError("get_thread_author", detail)

        return identifier
