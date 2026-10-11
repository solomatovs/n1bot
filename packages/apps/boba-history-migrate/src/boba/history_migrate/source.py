"""Чтение истории тредов из схемы `checkpoints` langgraph.

Источник только читается: список тредов — запросом к таблице checkpoints,
сообщения треда — штатным чекпойнтером AsyncPostgresSaver: канал `messages`
последнего checkpoint'а плюс ожидающие записи того же канала (итоги
инструментов оборванного хода). Сериализацию (msgpack, JsonPlusSerializer)
выполняет сам чекпойнтер, разбора байтов здесь нет.

Ошибки:
SourceError — база источника недоступна или отказала, либо checkpoint треда
    не читается чекпойнтером; причина нижнего слоя — в тексте и в `from exc`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

import psycopg
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple, PendingWrite
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder
from boba.db.postgres.errors import PostgresError
from boba.db.postgres.schema import PostgresSchema

__all__ = ["CheckpointSource", "SourceError", "ThreadTranscript"]


class SourceError(Exception):
    """Источник недоступен или checkpoint треда не читается."""


class CheckpointChannel(StrEnum):
    """Каналы состояния графа хода, которые читает миграция."""

    MESSAGES = "messages"


class CheckpointKey:
    """Ключи TypedDict Checkpoint langgraph: pyright принимает только литерал."""

    TS: Final = "ts"
    CHANNEL_VALUES: Final = "channel_values"


@dataclass(frozen=True)
class ThreadTranscript:
    """Сообщения одного треда из последнего checkpoint'а и его время."""

    thread_id: str
    stamp: str
    messages: Sequence[BaseMessage]


class CheckpointSource:
    """Читатель тредов схемы checkpoints на открытом пуле источника.

    Создаётся входом скрипта на пуле, чей search_path указывает на схему
    чекпойнтера: AsyncPostgresSaver пишет имена таблиц без схемы. Прогон
    (HistoryMigration) берёт у него список тредов и расшифровку каждого.
    """

    LABEL: ClassVar[str] = "checkpoints"

    def __init__(self, pool: AsyncPostgresPool, db_schema: str) -> None:
        self._pool = pool
        self._schema = PostgresSchema(db_schema)
        self._saver = AsyncPostgresSaver(pool.raw)

    async def thread_ids(self) -> Sequence[str]:
        """Идентификаторы тредов, у которых есть хотя бы один checkpoint."""
        query = (
            PgQueryBuilder(schema=self._schema.ident)
            .add(
                """
                select distinct
                    thread_id
                from
                    {schema}.checkpoints
                order by
                    thread_id
                """
            )
            .build()
        )
        try:
            async with self._pool.cursor() as cur:
                await cur.execute(query.text, query.params)
                rows = await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = (
                f"{self.LABEL}: listing threads in {self._schema.name}.checkpoints "
                f"failed: {exc}"
            )
            raise SourceError(msg) from exc

        ids: list[str] = []
        for row in rows:
            ids.append(str(row[0]))

        return ids

    async def transcript(self, thread_id: str) -> ThreadTranscript | None:
        """Сообщения треда; None — у треда нет checkpoint'а."""
        config = RunnableConfig(configurable={"thread_id": thread_id})
        try:
            snapshot = await self._saver.aget_tuple(config)
        except Exception as exc:
            msg = (
                f"{self.LABEL}: reading the last checkpoint of thread {thread_id} "
                f"from {self._schema.name} failed: {exc}"
            )
            raise SourceError(msg) from exc

        if snapshot is None:
            return None

        messages = list(self._stored(snapshot))
        messages.extend(self._pending(snapshot, messages))

        return ThreadTranscript(
            thread_id=thread_id,
            stamp=str(snapshot.checkpoint[CheckpointKey.TS]),
            messages=messages,
        )

    def _stored(self, snapshot: CheckpointTuple) -> Iterator[BaseMessage]:
        values = snapshot.checkpoint.get(CheckpointKey.CHANNEL_VALUES)
        if not isinstance(values, dict):
            return

        yield from self._written(values.get(CheckpointChannel.MESSAGES.value))

    def _pending(
        self, snapshot: CheckpointTuple, known: Sequence[BaseMessage]
    ) -> Iterator[BaseMessage]:
        """Сообщения из ожидающих записей: ответ задачи, доработавшей до
        конца узла, лежит только здесь."""
        writes: Iterable[PendingWrite] | None = snapshot.pending_writes
        if writes is None:
            return

        seen: set[str] = set()
        for message in known:
            if message.id:
                seen.add(message.id)

        for _task_id, channel, value in writes:
            if channel != CheckpointChannel.MESSAGES.value:
                continue

            for message in self._written(value):
                if message.id and message.id in seen:
                    continue

                yield message

    @staticmethod
    def _written(value: Any) -> Iterator[BaseMessage]:
        """Сообщения одной записи канала: узел отдаёт список или одно сообщение."""
        items: Sequence[Any] = [value]
        if isinstance(value, list):
            items = value

        for item in items:
            if not isinstance(item, BaseMessage):
                continue

            yield item
