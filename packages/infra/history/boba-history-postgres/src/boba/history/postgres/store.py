"""Реализация порта истории агента над Postgres (план, раздел 5.15).

Записи сессии лежат в таблице `history_records` по порядку seq, запись —
документ журнала как jsonb. Захват сессии — advisory lock сеанса по ключу
из идентификатора сессии на выделенном соединении, которое живёт до
release: второй процесс на ту же сессию получает отказ. Дозапись пачки —
одна транзакция с `on conflict do nothing`: повтор пачки дублей не создаёт,
отказ посреди пачки не оставляет ни одной записи. Список сессий — группировка
записей по идентификатору с временем последней записи; удаление сессии —
одна транзакция под транзакционным advisory lock того же ключа, так что
сессию работающего процесса удалить нельзя. Копия (ветвление) — одна
транзакция `insert … select` строк источника до записи среза под новым
идентификатором с `sessionId` цели в документе; замок источника не
берётся. Схема и таблица создаются на месте под замком DDL, раскладка
таблицы сверяется с ожидаемой.

Ошибки:
HistoryError — база недоступна или отказала, пачка не по порядку seq, у
    цели копии уже есть записи; причина нижнего слоя — в тексте и в
    `from exc`.
SessionHeldError — сессию держит другой процесс (подвид HistoryError).
SessionNotFoundError — у источника копии нет записей (подвид HistoryError).
RecordNotFoundError — записи среза копии в источнике нет (подвид
    HistoryError).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import psycopg
from psycopg.types.json import Jsonb

from boba.agent.history import (
    BrokenLine,
    HistoryError,
    HistoryLine,
    HistoryStore,
    LineKey,
    RecordNotFoundError,
    SessionHeldError,
    SessionId,
    SessionLease,
    SessionNotFoundError,
    StoredLine,
    StoredPage,
    StoredSession,
)
from boba.agent.records import Record, RecordCodec
from boba.db.postgres import (
    AsyncPostgresPool,
    Cursor,
    PgQuery,
    PostgresPool,
    PostgresTable,
)
from boba.db.postgres.connection import PostgresConfig

__all__ = ["HistoryColumn", "HistoryTable", "PostgresHistoryStore"]


class HistoryTable(StrEnum):
    """Имена объектов схемы истории."""

    RECORDS = "history_records"
    LOCK_PREFIX = "boba.history."


class HistoryColumn(StrEnum):
    """Колонки таблицы записей."""

    SESSION_ID = "session_id"
    SEQ = "seq"
    TYPE = "type"
    RECORD = "record"
    CREATED_AT = "created_at"


class ListColumn(StrEnum):
    """Колонки выборки списка сессий."""

    SESSION_ID = "session_id"
    UPDATED_AT = "updated_at"


class LockColumn(StrEnum):
    """Колонка пробы замка при удалении."""

    ACQUIRED = "acquired"


class CopyColumn(StrEnum):
    """Колонки выборок копии сессии."""

    LAST_SEQ = "last_seq"
    CUT_SEQ = "cut_seq"


@dataclass
class HeldSession:
    """Захваченная сессия: соединение с замком и последний сохранённый seq."""

    session: SessionId
    conn: psycopg.AsyncConnection[Any]
    last_seq: int


class PostgresHistoryStore(PostgresTable, HistoryStore):
    """Реализация HistoryStore над таблицей `history_records` схемы Postgres.

    Создаёт вход процесса по настройке `history` вида `postgres`; пул берётся
    по конфигу подключения при первом обращении, как у остальных таблиц.
    """

    LABEL: ClassVar[str] = "history"

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(postgres, db_schema, pool)
        self._config = postgres
        self._codec = RecordCodec()
        self._held: dict[str, HeldSession] = {}
        self._ready = False

    async def acquire(self, session: SessionId) -> SessionLease:
        await self._prepare()
        conn = await self._dedicated()
        try:
            await self._lock(conn, session)
            last_seq = await self._last_seq(conn, session)
        except HistoryError:
            await conn.close()
            raise
        except Exception as exc:
            await conn.close()
            raise self._failure(f"acquiring session '{session.value}'", exc) from exc

        lease = SessionLease(session=session, key=self._lease_key(session))
        self._held[lease.key] = HeldSession(
            session=session, conn=conn, last_seq=last_seq
        )

        return lease

    async def append(self, lease: SessionLease, records: Sequence[Record]) -> None:
        held = self._held_of(lease)
        fresh = list(self._fresh(held, records))
        if not fresh:
            return

        rows: list[dict[str, Any]] = []
        for record in fresh:
            rows.append(
                {
                    HistoryColumn.SESSION_ID.value: held.session.value,
                    HistoryColumn.SEQ.value: record.seq,
                    HistoryColumn.TYPE.value: record.type.value,
                    HistoryColumn.RECORD.value: Jsonb(self._document(record)),
                }
            )

        query = (
            self._query()
            .add(
                """
                insert into {schema}.history_records (session_id, seq, type, record)
                values (%(session_id)s, %(seq)s, %(type)s, %(record)s)
                on conflict (session_id, seq) do nothing
                """
            )
            .build()
        )
        action = f"appending {len(fresh)} record(s) to session '{held.session.value}'"
        async with self._transaction(action) as cur:
            await cur.executemany(query.text, rows)

        held.last_seq = fresh[-1].seq

    async def read(self, session: SessionId) -> AsyncIterator[HistoryLine]:
        await self._prepare()
        query = (
            self._query()
            .add(
                """
                select
                    seq,
                    record
                from
                    {schema}.history_records
                where
                    session_id = %(session_id)s
                order by
                    seq
                """,
                session_id=session.value,
            )
            .build()
        )
        rows = await self._rows(query, f"reading session '{session.value}'")
        for row in rows:
            yield self._line(
                row[HistoryColumn.SEQ.value], row[HistoryColumn.RECORD.value]
            )

    async def release(self, lease: SessionLease) -> None:
        held = self._held.pop(lease.key, None)
        if held is None:
            return

        try:
            await self._unlock(held.conn, held.session)
        except psycopg.Error as exc:
            msg = f"Could not release session '{held.session.value}' in Postgres: {exc}"
            raise HistoryError(msg) from exc
        finally:
            await held.conn.close()

    async def list(self, cursor: str | None, limit: int) -> StoredPage:
        await self._prepare()
        query = (
            self._query()
            .add(
                """
                select
                    session_id,
                    max(record ->> %(stamp)s) as updated_at
                from
                    {schema}.history_records
                """,
                stamp=LineKey.TIMESTAMP.value,
            )
            .when(cursor is not None, "where session_id > %(cursor)s", cursor=cursor)
            .add(
                """
                group by
                    session_id
                order by
                    session_id
                limit %(limit)s
                """,
                limit=limit + 1,
            )
            .build()
        )
        rows = await self._rows(query, f"listing sessions after {cursor!r}")
        sessions: list[StoredSession] = []
        for row in rows[:limit]:
            sessions.append(
                StoredSession(
                    session=SessionId(value=str(row[ListColumn.SESSION_ID.value])),
                    updated_at=str(row[ListColumn.UPDATED_AT.value]),
                )
            )

        next_cursor: str | None = None
        if len(rows) > limit and sessions:
            next_cursor = sessions[-1].session.value

        return StoredPage(sessions=sessions, next_cursor=next_cursor)

    async def delete(self, session: SessionId) -> None:
        await self._prepare()
        probe = (
            self._query()
            .add(
                "select pg_try_advisory_xact_lock(hashtextextended(%(key)s, 0)) "
                "as acquired",
                key=self._lock_key(session),
            )
            .build()
        )
        query = (
            self._query()
            .add(
                """
                delete from
                    {schema}.history_records
                where
                    session_id = %(session_id)s
                """,
                session_id=session.value,
            )
            .build()
        )
        action = f"deleting session '{session.value}'"
        async with self._transaction(action) as cur:
            await cur.execute(probe.text, probe.params)
            row = await cur.fetchone()
            if row is None or not row[LockColumn.ACQUIRED.value]:
                msg = (
                    f"Session '{session.value}' cannot be deleted: a running agent "
                    "process holds its lock in Postgres. Close that session first."
                )
                raise SessionHeldError(msg)

            await cur.execute(query.text, query.params)

    async def copy(
        self, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        await self._prepare()
        action = f"copying session '{source.value}' to '{target.value}'"
        async with self._transaction(action) as cur:
            source_last = await self._max_seq(cur, source)
            if source_last == 0:
                msg = (
                    f"Session '{source.value}' cannot be copied: it has no records "
                    "in Postgres, there is nothing to branch."
                )
                raise SessionNotFoundError(msg)

            end: int | None = None
            if up_to is not None:
                end = await self._seq_of(cur, source, up_to)

            target_last = await self._max_seq(cur, target)
            if target_last != 0:
                msg = (
                    f"Session '{target.value}' cannot receive the copy of "
                    f"'{source.value}': it already has records up to seq "
                    f"{target_last} in Postgres."
                )
                raise HistoryError(msg)

            query = (
                self._query()
                .add(
                    """
                    insert into {schema}.history_records (session_id, seq, type, record)
                    select
                        %(target)s,
                        seq,
                        type,
                        jsonb_set(record, %(path)s, to_jsonb(%(target)s::text))
                    from
                        {schema}.history_records
                    where
                        session_id = %(source)s
                    """,
                    target=target.value,
                    path=[LineKey.SESSION_ID.value],
                    source=source.value,
                )
                .when(end is not None, "and seq < %(end)s", end=end)
                .add("order by seq")
                .build()
            )
            await cur.execute(query.text, query.params)

            return cur.rowcount

    async def _max_seq(self, cur: Cursor, session: SessionId) -> int:
        query = (
            self._query()
            .add(
                """
                select
                    coalesce(max(seq), 0) as last_seq
                from
                    {schema}.history_records
                where
                    session_id = %(session_id)s
                """,
                session_id=session.value,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            return 0

        return int(row[CopyColumn.LAST_SEQ.value])

    async def _seq_of(self, cur: Cursor, session: SessionId, uuid: str) -> int:
        """Ошибки:
        RecordNotFoundError — записи с таким uuid в сессии нет.
        """
        query = (
            self._query()
            .add(
                """
                select
                    seq as cut_seq
                from
                    {schema}.history_records
                where
                    session_id = %(session_id)s
                    and record ->> %(key)s = %(uuid)s
                """,
                session_id=session.value,
                key=LineKey.UUID.value,
                uuid=uuid,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            msg = (
                f"Session '{session.value}' cannot be branched at record '{uuid}': "
                "it has no record with that uuid in Postgres."
            )
            raise RecordNotFoundError(msg)

        return int(row[CopyColumn.CUT_SEQ.value])

    def _failure(self, action: str, exc: Exception) -> Exception:
        return HistoryError(self._detail(action, exc))

    async def _prepare(self) -> None:
        """Схема, таблица и сверка раскладки — один раз на хранилище."""
        if self._ready:
            return

        await self._apply_ddl(list(self._ddl()))
        await self._check_layouts(
            {HistoryTable.RECORDS.value: [column.value for column in HistoryColumn]}
        )
        self._ready = True

    def _ddl(self) -> Iterator[PgQuery]:
        yield (
            self._query()
            .add(
                """
                create table if not exists {schema}.history_records (
                    session_id text        not null,
                    seq        integer     not null,
                    type       text        not null,
                    record     jsonb       not null,
                    created_at timestamptz not null default now(),
                    primary key (session_id, seq)
                )
                """
            )
            .build()
        )
        yield (
            self._query()
            .add(
                "comment on table {schema}.history_records is "
                "'Журнал сессий агента: по записи на строку, в порядке seq внутри "
                "сессии; документ записи такой же, как строка журнала JSONL'"
            )
            .build()
        )
        yield (
            self._query()
            .add(
                "comment on column {schema}.history_records.seq is "
                "'Порядковый номер записи в сессии с 1; выдаёт сессия агента'"
            )
            .build()
        )
        yield (
            self._query()
            .add(
                "comment on column {schema}.history_records.type is "
                "'Вид записи: user, assistant, attachment, system, queue-operation, "
                "permission-mode, permission-rule, api-request'"
            )
            .build()
        )

    async def _dedicated(self) -> psycopg.AsyncConnection[Any]:
        """Соединение замка вне пула: замок сеанса живёт, пока живо оно."""
        async with self._guarded("opening the lock connection"):
            return await AsyncPostgresPool.dedicated(self._config)

    async def _lock(
        self, conn: psycopg.AsyncConnection[Any], session: SessionId
    ) -> None:
        """Ошибки:
        SessionHeldError — сессию держит другой процесс.
        """
        query = (
            self._query()
            .add(
                "select pg_try_advisory_lock(hashtextextended(%(key)s, 0))",
                key=self._lock_key(session),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None or not row[0]:
            msg = (
                f"Session '{session.value}' is already in use by another process: its "
                "lock in Postgres is held. Stop that process or use another session."
            )
            raise SessionHeldError(msg)

    async def _unlock(
        self, conn: psycopg.AsyncConnection[Any], session: SessionId
    ) -> None:
        query = (
            self._query()
            .add(
                "select pg_advisory_unlock(hashtextextended(%(key)s, 0))",
                key=self._lock_key(session),
            )
            .build()
        )
        await conn.execute(query.text, query.params)

    async def _last_seq(
        self, conn: psycopg.AsyncConnection[Any], session: SessionId
    ) -> int:
        query = (
            self._query()
            .add(
                """
                select
                    coalesce(max(seq), 0)
                from
                    {schema}.history_records
                where
                    session_id = %(session_id)s
                """,
                session_id=session.value,
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            return 0

        return int(row[0])

    @staticmethod
    def _lock_key(session: SessionId) -> str:
        return HistoryTable.LOCK_PREFIX.value + session.value

    @staticmethod
    def _lease_key(session: SessionId) -> str:
        return session.value

    def _held_of(self, lease: SessionLease) -> HeldSession:
        held = self._held.get(lease.key)
        if held is None:
            msg = (
                f"Session '{lease.session.value}' was not acquired through this "
                "history store, so its lease is unknown here. Acquire the session "
                "first."
            )
            raise HistoryError(msg)

        return held

    @staticmethod
    def _fresh(held: HeldSession, records: Sequence[Record]) -> Iterator[Record]:
        """Записи с seq новее сохранённого, строго по возрастанию."""
        last = held.last_seq
        for record in records:
            if record.seq <= held.last_seq:
                continue

            if record.seq <= last:
                msg = (
                    f"Cannot append to session '{held.session.value}': the batch is "
                    f"out of order, record seq {record.seq} comes after seq {last}."
                )
                raise HistoryError(msg)

            last = record.seq
            yield record

    def _document(self, record: Record) -> Mapping[str, Any]:
        """Документ записи — ровно то, что лежало бы строкой журнала JSONL."""
        return json.loads(self._codec.line(record))

    @staticmethod
    def _line(seq: object, document: object) -> HistoryLine:
        number = int(str(seq))
        if isinstance(document, Mapping):
            return StoredLine(number=number, raw=document)

        return BrokenLine(
            number=number,
            reason=f"the record is not a JSON object but {type(document).__name__}",
        )
