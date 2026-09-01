"""Таблицы elements и feedbacks чата: единственный владелец их DDL и SQL.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from boba.chat.threads import (
    ChatTable,
    ElementsColumn,
    ElementStore,
    FeedbacksColumn,
    FeedbackStore,
    StoredElement,
    StoredFeedback,
)
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, SqlNames
from boba.runtime.table import PgTable
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable

__all__ = ["ChatTables", "ElementsTable", "FeedbacksTable"]


class ElementsTable(PgTable, ElementStore):
    """elements треда: строка описания вложения, тело — в хранилище файлов."""

    def _elements(self) -> sql.Identifier:
        return SqlNames.table(self._schema, ChatTable.ELEMENTS)

    @staticmethod
    def _columns() -> sql.Composed:
        return sql.SQL(", ").join([SqlNames.ident(column) for column in ElementsColumn])

    @staticmethod
    def _stored(row: tuple[Any, ...]) -> StoredElement:
        names = [column.value for column in ElementsColumn]
        values = dict(zip(names, row, strict=True))
        for key in ("chainlit_key", "size", "language", "mime"):
            if values[key] is None:
                values[key] = ""
        if values["props"] is None:
            values["props"] = {}

        return StoredElement.model_validate(values)

    async def setup(self) -> None:
        ddl = (
            sql.SQL(
                """
                create table if not exists {elements} (
                    {id}           uuid primary key,
                    {name}         text not null,
                    {type}         text not null,
                    {display}      text not null,
                    {thread_id}    uuid,
                    {for_id}       uuid,
                    {chainlit_key} text,
                    {size}         text,
                    {language}     text,
                    {page}         integer,
                    {props}        jsonb,
                    {mime}         text
                )
                """
            ).format(
                elements=self._elements(),
                **{column.value: SqlNames.ident(column) for column in ElementsColumn},
            ),
            sql.SQL(
                """
                create index if not exists idx_elements_thread_id
                    on {elements} ({thread_id})
                """
            ).format(
                elements=self._elements(),
                thread_id=SqlNames.ident(ElementsColumn.THREAD_ID),
            ),
        )

        await self._run(ddl, "elements.setup")

    async def upsert(self, element: StoredElement) -> None:
        assignments = sql.SQL(", ").join(
            [
                sql.SQL("{0} = excluded.{0}").format(SqlNames.ident(column))
                for column in ElementsColumn
                if column is not ElementsColumn.ID
            ]
        )
        query = sql.SQL(
            """
            insert into {elements} ({cols})
            values ({placeholders})
            on conflict ({id})
            do update set {assignments}
            """
        ).format(
            elements=self._elements(),
            cols=self._columns(),
            placeholders=sql.SQL(", ").join(
                [sql.Placeholder(column.value) for column in ElementsColumn]
            ),
            id=SqlNames.ident(ElementsColumn.ID),
            assignments=assignments,
        )
        params: dict[str, Any] = element.model_dump()
        params["props"] = Jsonb(dict(element.props))
        for key in (
            ElementsColumn.CHAINLIT_KEY,
            ElementsColumn.SIZE,
            ElementsColumn.LANGUAGE,
            ElementsColumn.MIME,
        ):
            if params[key] == "":
                params[key] = None

        await self._execute_as(query, params, "create_element")

    async def find(self, element_id: UUID) -> StoredElement | None:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where
                {id} = %(id)s
            """
        ).format(
            cols=self._columns(),
            elements=self._elements(),
            id=SqlNames.ident(ElementsColumn.ID),
        )
        rows = await self._fetch_as(
            query, {ElementsColumn.ID: element_id}, "get_element"
        )
        if not rows:
            return None

        return self._stored(rows[0])

    async def get(self, thread_id: UUID, element_id: UUID) -> StoredElement | None:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where 1=1
                and {thread_id} = %(thread_id)s
                and {id} = %(id)s
            """
        ).format(
            cols=self._columns(),
            elements=self._elements(),
            thread_id=SqlNames.ident(ElementsColumn.THREAD_ID),
            id=SqlNames.ident(ElementsColumn.ID),
        )
        rows = await self._fetch_as(
            query,
            {ElementsColumn.THREAD_ID: thread_id, ElementsColumn.ID: element_id},
            "get_element",
        )
        if not rows:
            return None

        return self._stored(rows[0])

    async def delete(self, element_id: UUID) -> StoredElement | None:
        query = sql.SQL(
            """
            delete from {elements}
            where
                {id} = %(id)s
            returning
                {cols}
            """
        ).format(
            elements=self._elements(),
            id=SqlNames.ident(ElementsColumn.ID),
            cols=self._columns(),
        )
        rows = await self._fetch_as(
            query, {ElementsColumn.ID: element_id}, "delete_element"
        )
        if not rows:
            return None

        return self._stored(rows[0])

    async def list_of_thread(self, thread_id: UUID) -> Sequence[StoredElement]:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where
                {thread_id} = %(thread_id)s
            """
        ).format(
            cols=self._columns(),
            elements=self._elements(),
            thread_id=SqlNames.ident(ElementsColumn.THREAD_ID),
        )
        rows = await self._fetch_as(
            query, {ElementsColumn.THREAD_ID: thread_id}, "get_thread"
        )

        return [self._stored(row) for row in rows]

    async def delete_of_thread(self, thread_id: UUID) -> None:
        query = sql.SQL(
            """
            delete from {elements}
            where
                {thread_id} = %(thread_id)s
            """
        ).format(
            elements=self._elements(),
            thread_id=SqlNames.ident(ElementsColumn.THREAD_ID),
        )

        await self._execute_as(
            query, {ElementsColumn.THREAD_ID: thread_id}, "delete_thread"
        )

    async def delete_of_step(self, step_id: UUID) -> None:
        query = sql.SQL(
            """
            delete from {elements}
            where
                {for_id} = %(for_id)s
            """
        ).format(
            elements=self._elements(), for_id=SqlNames.ident(ElementsColumn.FOR_ID)
        )

        await self._execute_as(query, {ElementsColumn.FOR_ID: step_id}, "delete_step")


class FeedbacksTable(PgTable, FeedbackStore):
    """feedbacks: оценка шага пользователем."""

    def _feedbacks(self) -> sql.Identifier:
        return SqlNames.table(self._schema, ChatTable.FEEDBACKS)

    @staticmethod
    def _columns() -> sql.Composed:
        return sql.SQL(", ").join(
            [SqlNames.ident(column) for column in FeedbacksColumn]
        )

    @staticmethod
    def _stored(row: tuple[Any, ...]) -> StoredFeedback:
        comment = row[4]
        if comment is None:
            comment = ""

        return StoredFeedback(
            id=row[0], for_id=row[1], value=row[2], thread_id=row[3], comment=comment
        )

    async def setup(self) -> None:
        ddl = (
            sql.SQL(
                """
                create table if not exists {feedbacks} (
                    {id}        uuid primary key,
                    {for_id}    uuid not null,
                    {value}     smallint not null,
                    {thread_id} uuid,
                    {comment}   text
                )
                """
            ).format(
                feedbacks=self._feedbacks(),
                **{column.value: SqlNames.ident(column) for column in FeedbacksColumn},
            ),
            sql.SQL(
                """
                create index if not exists idx_feedbacks_for_id
                    on {feedbacks} ({for_id})
                """
            ).format(
                feedbacks=self._feedbacks(),
                for_id=SqlNames.ident(FeedbacksColumn.FOR_ID),
            ),
        )

        await self._run(ddl, "feedbacks.setup")

    async def upsert(self, feedback: StoredFeedback) -> None:
        query = sql.SQL(
            """
            insert into {feedbacks} ({id}, {for_id}, {value}, {thread_id}, {comment})
            values (%(id)s, %(for_id)s, %(value)s, %(thread_id)s, %(comment)s)
            on conflict ({id}) do update set
                {for_id}    = excluded.{for_id},
                {value}     = excluded.{value},
                {thread_id} = excluded.{thread_id},
                {comment}   = excluded.{comment}
            """
        ).format(
            feedbacks=self._feedbacks(),
            **{column.value: SqlNames.ident(column) for column in FeedbacksColumn},
        )
        params: dict[str, Any] = feedback.model_dump()
        if params["comment"] == "":
            params["comment"] = None

        await self._execute_as(query, params, "upsert_feedback")

    async def delete(self, feedback_id: UUID) -> StoredFeedback | None:
        query = sql.SQL(
            """
            delete from {feedbacks}
            where
                {id} = %(id)s
            returning {cols}
            """
        ).format(
            feedbacks=self._feedbacks(),
            id=SqlNames.ident(FeedbacksColumn.ID),
            cols=self._columns(),
        )
        rows = await self._fetch_as(
            query, {FeedbacksColumn.ID: feedback_id}, "delete_feedback"
        )
        if not rows:
            return None

        return self._stored(rows[0])

    async def list_of_thread(self, thread_id: UUID) -> Sequence[StoredFeedback]:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {feedbacks}
            where
                {thread_id} = %(thread_id)s
            """
        ).format(
            cols=self._columns(),
            feedbacks=self._feedbacks(),
            thread_id=SqlNames.ident(FeedbacksColumn.THREAD_ID),
        )
        rows = await self._fetch_as(
            query, {FeedbacksColumn.THREAD_ID: thread_id}, "get_thread"
        )

        return [self._stored(row) for row in rows]

    async def delete_of_thread(self, thread_id: UUID) -> None:
        query = sql.SQL(
            """
            delete from {feedbacks}
            where
                {thread_id} = %(thread_id)s
            """
        ).format(
            feedbacks=self._feedbacks(),
            thread_id=SqlNames.ident(FeedbacksColumn.THREAD_ID),
        )

        await self._execute_as(
            query, {FeedbacksColumn.THREAD_ID: thread_id}, "delete_thread"
        )

    async def delete_of_step(self, step_id: UUID) -> None:
        query = sql.SQL(
            """
            delete from {feedbacks}
            where
                {for_id} = %(for_id)s
            """
        ).format(
            feedbacks=self._feedbacks(), for_id=SqlNames.ident(FeedbacksColumn.FOR_ID)
        )

        await self._execute_as(query, {FeedbacksColumn.FOR_ID: step_id}, "delete_step")


class ChatTables:
    """Четыре таблицы схемы чата одним набором: сборка на общем пуле и DDL разом."""

    def __init__(
        self,
        users: UsersTable,
        threads: ThreadsTable,
        elements: ElementsTable,
        feedbacks: FeedbacksTable,
    ) -> None:
        self.users = users
        self.threads = threads
        self.elements = elements
        self.feedbacks = feedbacks

    @classmethod
    def of(
        cls, postgres: PostgresConfig, db_schema: str, pool: AsyncPostgresPool
    ) -> ChatTables:
        return cls(
            users=UsersTable(postgres, db_schema, pool),
            threads=ThreadsTable(postgres, db_schema, pool),
            elements=ElementsTable(postgres, db_schema, pool),
            feedbacks=FeedbacksTable(postgres, db_schema, pool),
        )

    @classmethod
    def around(
        cls,
        users: UsersTable,
        postgres: PostgresConfig,
        db_schema: str,
        pool: AsyncPostgresPool,
    ) -> ChatTables:
        """Таблицы чата вокруг уже существующей users: одна строка users на процесс."""
        return cls(
            users=users,
            threads=ThreadsTable(postgres, db_schema, pool),
            elements=ElementsTable(postgres, db_schema, pool),
            feedbacks=FeedbacksTable(postgres, db_schema, pool),
        )

    async def setup(self) -> None:
        await self.users.setup()
        await self.threads.setup()
        await self.elements.setup()
        await self.feedbacks.setup()
