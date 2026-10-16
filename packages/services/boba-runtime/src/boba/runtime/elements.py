"""Таблица feedbacks чата: единственный владелец её SQL.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from boba.chat.threads import (
    FeedbacksColumn,
    FeedbackStore,
    StoredFeedback,
)
from boba.db.postgres import PgQueryBuilder
from boba.runtime.table import PgTable

__all__ = ["FeedbacksTable"]


class FeedbacksTable(PgTable, FeedbackStore):
    """feedbacks: оценка шага пользователем."""

    def _query(self) -> PgQueryBuilder:
        return PgQueryBuilder(
            schema=self._schema.ident, columns=self._column_list(FeedbacksColumn)
        )

    def _stored(self, row: Mapping[str, Any]) -> StoredFeedback:
        comment = row[FeedbacksColumn.COMMENT.value]
        if comment is None:
            comment = ""

        return StoredFeedback(
            id=row[FeedbacksColumn.ID.value],
            for_id=row[FeedbacksColumn.FOR_ID.value],
            value=row[FeedbacksColumn.VALUE.value],
            thread_id=row[FeedbacksColumn.THREAD_ID.value],
            comment=comment,
        )

    async def upsert(self, feedback: StoredFeedback) -> None:
        params: dict[str, Any] = feedback.model_dump()
        if params[FeedbacksColumn.COMMENT.value] == "":
            params[FeedbacksColumn.COMMENT.value] = None

        query = (
            self._query()
            .add(
                """
                insert into {schema}.feedbacks ({columns})
                values (%(id)s, %(for_id)s, %(value)s, %(thread_id)s, %(comment)s)
                on conflict (id) do update set
                    for_id    = excluded.for_id,
                    value     = excluded.value,
                    thread_id = excluded.thread_id,
                    comment   = excluded.comment
                """,
                **params,
            )
            .build()
        )

        await self._execute(query, "upsert_feedback")

    async def delete(self, feedback_id: UUID) -> StoredFeedback | None:
        query = (
            self._query()
            .add(
                """
                delete from {schema}.feedbacks
                where
                    id = %(id)s
                returning {columns}
                """,
                id=feedback_id,
            )
            .build()
        )
        row = await self._row(query, "delete_feedback")
        if row is None:
            return None

        return self._stored(row)

    async def list_of_thread(self, thread_id: UUID) -> Sequence[StoredFeedback]:
        query = (
            self._query()
            .add(
                """
                select
                    {columns}
                from
                    {schema}.feedbacks
                where
                    thread_id = %(thread_id)s
                """,
                thread_id=thread_id,
            )
            .build()
        )
        rows = await self._rows(query, "get_thread")

        feedbacks: list[StoredFeedback] = []
        for row in rows:
            feedbacks.append(self._stored(row))

        return feedbacks

    async def delete_of_thread(self, thread_id: UUID) -> None:
        query = (
            self._query()
            .add(
                "delete from {schema}.feedbacks where thread_id = %(thread_id)s",
                thread_id=thread_id,
            )
            .build()
        )

        await self._execute(query, "delete_thread")
