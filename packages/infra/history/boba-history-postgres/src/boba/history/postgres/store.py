"""Реализация порта истории агента над Postgres (план, раздел 5.15).

Сессия — строка таблицы `session`, её записи — строки таблицы `history` по
порядку seq. Каркас, который есть у каждой записи (сессия, seq, вид, время),
лежит колонками, остальное содержимое записи — документом jsonb; при чтении
документ собирается обратно таким, каким он лёг бы строкой журнала JSONL.
Захват сессии — advisory lock сеанса по ключу из идентификатора сессии на
выделенном соединении, которое живёт до release: второй процесс на ту же
сессию получает отказ. Дозапись пачки — одна транзакция: строку сессии
заводит первая пачка, записи идут с `on conflict do nothing`, поэтому повтор
пачки дублей не создаёт, а отказ посреди пачки не оставляет ни одной записи.
Список сессий — строки `session` со временем последней записи; удаление
сессии — удаление её строки под транзакционным advisory lock того же ключа,
записи уходят каскадом, а сессию работающего процесса удалить нельзя. Копия
(ветвление) — одна транзакция `insert … select` записей источника до записи
среза под новым идентификатором; замок источника не берётся. Схема и таблицы
создаются файлами schema/*.sql пакета под замком DDL (колонка владельца
добавляется к таблицам прежнего выпуска на месте), раскладка таблиц
сверяется с ожидаемой.

Хранилище с владельцем (логин пользователя, от имени которого работает
процесс) заводит сессии на него и видит только его сессии: чужая сессия и
сессия без владельца для него не существуют — чтение пусто, в списке её нет,
захват, копия и удаление отвечают как на сессию без записей. Хранилище без
владельца (процесс, запущенный по stdio) видит все сессии и заводит новые
без владельца.

Ошибки:
HistoryError — база недоступна или отказала, пачка не по порядку seq или с
    записью чужой сессии, у цели копии уже есть записи; причина нижнего
    слоя — в тексте и в `from exc`.
SessionHeldError — сессию держит другой процесс (подвид HistoryError).
SessionNotFoundError — у источника копии нет записей (подвид HistoryError).
RecordNotFoundError — записи среза копии в источнике нет (подвид
    HistoryError).
"""

from __future__ import annotations

import json
from collections.abc import (
    AsyncIterator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
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
    PostgresError,
    PostgresPool,
    PostgresTable,
)
from boba.db.postgres.connection import PostgresConfig

__all__ = [
    "HistoryColumn",
    "HistoryTable",
    "PostgresHistoryStore",
    "RecordFrames",
    "SessionColumn",
]


class HistoryTable(StrEnum):
    """Имена объектов схемы истории."""

    SESSION = "session"
    HISTORY = "history"
    LOCK_PREFIX = "boba.history."


class SessionColumn(StrEnum):
    """Колонки таблицы сессий."""

    ID = "id"
    CREATED_AT = "created_at"
    OWNER = "owner"


class HistoryColumn(StrEnum):
    """Колонки таблицы записей."""

    SESSION_ID = "session_id"
    SEQ = "seq"
    TYPE = "type"
    RECORDED_AT = "recorded_at"
    RECORD = "record"


class FrameKey(StrEnum):
    """Ключи каркаса в документе записи: в таблице они лежат колонками."""

    TYPE = RecordCodec.TYPE
    SEQ = LineKey.SEQ.value
    SESSION_ID = LineKey.SESSION_ID.value
    TIMESTAMP = LineKey.TIMESTAMP.value


class ListColumn(StrEnum):
    """Колонки выборки списка сессий."""

    SESSION_ID = "session_id"
    UPDATED_AT = "updated_at"


class ProbeColumn(StrEnum):
    """Колонки служебных выборок: проба замка, наличие сессии, seq среза."""

    ACQUIRED = "acquired"
    PRESENT = "present"
    CUT_SEQ = "cut_seq"


@dataclass(frozen=True)
class HistoryRow:
    """Запись, разложенная для таблицы history: каркас и остальной документ."""

    seq: int
    type: str
    recorded_at: str
    record: Mapping[str, Any]


class RecordFrames:
    """Раскладка записи журнала по колонкам таблицы history и сборка обратно.

    PostgresHistoryStore держит каркас записи (сессию, seq, вид, время)
    колонками, а остальное — документом record. Этот класс снимает каркас с
    документа при записи и возвращает его при чтении, так что загрузчик агента
    получает тот же документ, что и из журнала JSONL, и о раскладке таблицы
    не знает.
    """

    def __init__(self) -> None:
        self._codec = RecordCodec()

    def split(self, session: SessionId, record: Record) -> HistoryRow:
        """Ошибки:
        HistoryError — запись принадлежит другой сессии.
        """
        if record.session_id != session.value:
            msg = (
                f"Cannot append to session '{session.value}': record seq "
                f"{record.seq} carries sessionId '{record.session_id}', and the "
                "history keeps one session id per row."
            )
            raise HistoryError(msg)

        document = json.loads(self._codec.line(record))
        for key in FrameKey:
            document.pop(key.value)

        return HistoryRow(
            seq=record.seq,
            type=record.type.value,
            recorded_at=record.timestamp,
            record=document,
        )

    def join(self, row: Mapping[str, Any]) -> HistoryLine:
        number = int(str(row[HistoryColumn.SEQ.value]))
        record = row[HistoryColumn.RECORD.value]
        if not isinstance(record, Mapping):
            return BrokenLine(
                number=number,
                reason=f"the record is not a JSON object but {type(record).__name__}",
            )

        document: dict[str, Any] = dict(record)
        document[FrameKey.TYPE.value] = row[HistoryColumn.TYPE.value]
        document[FrameKey.SEQ.value] = number
        document[FrameKey.SESSION_ID.value] = row[HistoryColumn.SESSION_ID.value]
        document[FrameKey.TIMESTAMP.value] = row[HistoryColumn.RECORDED_AT.value]

        return StoredLine(number=number, raw=document)


@dataclass
class HeldSession:
    """Захваченная сессия: соединение с замком и последний сохранённый seq."""

    session: SessionId
    conn: psycopg.AsyncConnection[Any]
    last_seq: int


class PostgresHistoryStore(PostgresTable, HistoryStore):
    """Реализация HistoryStore над таблицами `session` и `history` схемы Postgres.

    Создаёт вход процесса по настройке `history` вида `postgres`; пул берётся
    по конфигу подключения при первом обращении, как у остальных таблиц.
    Раскладку записи по колонкам и сборку обратно ведёт RecordFrames.
    owner — логин владельца сессий процесса (ключ `history.owner`); None —
    процесс без владельца, ему видны все сессии.
    """

    LABEL: ClassVar[str] = "history"
    STAMP: ClassVar[str] = 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
    """Время записи в UTC шаблоном to_char: так его пишет часовой агента."""

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        owner: str | None,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(postgres, db_schema, pool)
        self._config = postgres
        self._owner = owner
        self._frames = RecordFrames()
        self._held: dict[str, HeldSession] = {}
        self._ready = False

    async def acquire(self, session: SessionId) -> SessionLease:
        await self._prepare()
        await self._check_owner(session)
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

        rows = list(self._rows_of(held.session, fresh))

        opening = (
            self._query()
            .add(
                """
                insert into {schema}.session (id, created_at, owner)
                values (%(session_id)s, %(created_at)s, %(owner)s)
                on conflict (id) do nothing
                """,
                session_id=held.session.value,
                created_at=fresh[0].timestamp,
                owner=self._owner,
            )
            .build()
        )
        query = (
            self._query()
            .add(
                """
                insert into {schema}.history
                    (session_id, seq, type, recorded_at, record)
                values
                    (%(session_id)s, %(seq)s, %(type)s, %(recorded_at)s, %(record)s)
                on conflict (session_id, seq) do nothing
                """
            )
            .build()
        )
        action = f"appending {len(fresh)} record(s) to session '{held.session.value}'"
        async with self._transaction(action) as cur:
            await cur.execute(opening.text, opening.params)
            await cur.executemany(query.text, rows)

        held.last_seq = fresh[-1].seq

    async def read(self, session: SessionId) -> AsyncIterator[HistoryLine]:
        await self._prepare()
        query = (
            self._query()
            .add(
                """
                select
                    h.session_id,
                    h.seq,
                    h.type,
                    to_char(h.recorded_at at time zone 'UTC', %(stamp)s)
                        as recorded_at,
                    h.record
                from
                    {schema}.history as h
                    join {schema}.session as s on s.id = h.session_id
                where
                    h.session_id = %(session_id)s
                """,
                stamp=self.STAMP,
                session_id=session.value,
            )
            .when(self._owned(), "and s.owner = %(owner)s", owner=self._owner)
            .add(
                """
                order by
                    h.seq
                """
            )
            .build()
        )
        rows = await self._rows(query, f"reading session '{session.value}'")
        for row in rows:
            yield self._frames.join(row)

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
                    s.id as session_id,
                    to_char(tail.recorded_at at time zone 'UTC', %(stamp)s)
                        as updated_at
                from
                    {schema}.session as s
                    cross join lateral (
                        select
                            h.recorded_at
                        from
                            {schema}.history as h
                        where
                            h.session_id = s.id
                        order by
                            h.seq desc
                        limit 1
                    ) as tail
                where
                    true
                """,
                stamp=self.STAMP,
            )
            .when(self._owned(), "and s.owner = %(owner)s", owner=self._owner)
            .when(cursor is not None, "and s.id > %(cursor)s", cursor=cursor)
            .add(
                """
                order by
                    s.id
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

        if len(rows) <= limit:
            return StoredPage(sessions=sessions, next_cursor=None)

        if not sessions:
            return StoredPage(sessions=sessions, next_cursor=None)

        return StoredPage(sessions=sessions, next_cursor=sessions[-1].session.value)

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
                    {schema}.session
                where
                    id = %(session_id)s
                """,
                session_id=session.value,
            )
            .when(self._owned(), "and owner = %(owner)s", owner=self._owner)
            .build()
        )
        action = f"deleting session '{session.value}'"
        async with self._transaction(action) as cur:
            await cur.execute(probe.text, probe.params)
            row = await cur.fetchone()
            acquired = False
            if row is not None:
                acquired = bool(row[ProbeColumn.ACQUIRED.value])

            if not acquired:
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
            if not await self._exists(cur, source, self._owned()):
                msg = (
                    f"Session '{source.value}' cannot be copied: it has no records "
                    "in Postgres, there is nothing to branch."
                )
                raise SessionNotFoundError(msg)

            end: int | None = None
            if up_to is not None:
                end = await self._seq_of(cur, source, up_to)

            if await self._exists(cur, target, False):
                msg = (
                    f"Session '{target.value}' cannot receive the copy of "
                    f"'{source.value}': it already has records in Postgres."
                )
                raise HistoryError(msg)

            query = (
                self._query()
                .add(
                    """
                    with copied as (
                        select
                            seq,
                            type,
                            recorded_at,
                            record
                        from
                            {schema}.history
                        where
                            session_id = %(source)s
                    """,
                    source=source.value,
                )
                .when(end is not None, "and seq < %(end)s", end=end)
                .add(
                    """
                    ),
                    opened as (
                        insert into {schema}.session (id, created_at, owner)
                        select
                            %(target)s,
                            min(recorded_at),
                            %(owner)s
                        from
                            copied
                        having
                            count(*) > 0
                    )
                    insert into {schema}.history
                        (session_id, seq, type, recorded_at, record)
                    select
                        %(target)s,
                        seq,
                        type,
                        recorded_at,
                        record
                    from
                        copied
                    """,
                    target=target.value,
                    owner=self._owner,
                )
                .build()
            )
            await cur.execute(query.text, query.params)

            return cur.rowcount

    async def _exists(self, cur: Cursor, session: SessionId, owned: bool) -> bool:
        """Есть ли строка сессии: её заводит первая запись, удаление уносит;
        owned — только среди сессий владельца хранилища."""
        query = (
            self._query()
            .add(
                """
                select
                    exists (
                        select
                            1
                        from
                            {schema}.session
                        where
                            id = %(session_id)s
                """,
                session_id=session.value,
            )
            .when(owned, "and owner = %(owner)s", owner=self._owner)
            .add(
                """
                    ) as present
                """
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            return False

        return bool(row[ProbeColumn.PRESENT.value])

    def _owned(self) -> bool:
        """Хранилище видит только сессии своего владельца."""
        return self._owner is not None

    async def _check_owner(self, session: SessionId) -> None:
        """Захват сессии, заведённой на другого владельца или без него,
        отвечает так же, как на сессию без записей: чужая сессия не видна.

        Ошибки:
        SessionNotFoundError — сессия есть, но принадлежит не владельцу хранилища.
        HistoryError — база отказала.
        """
        if not self._owned():
            return

        query = (
            self._query()
            .add(
                """
                select
                    exists (
                        select
                            1
                        from
                            {schema}.session
                        where
                            id = %(session_id)s
                            and owner is distinct from %(owner)s
                    ) as present
                """,
                session_id=session.value,
                owner=self._owner,
            )
            .build()
        )
        action = f"checking the owner of session '{session.value}'"
        row = await self._row(query, action)
        foreign = False
        if row is not None:
            foreign = bool(row[ProbeColumn.PRESENT.value])

        if foreign:
            msg = (
                f"Session '{session.value}' is not found among the sessions of "
                f"'{self._owner}': it has no records this process may open."
            )
            raise SessionNotFoundError(msg)

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
                    {schema}.history
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

        return int(row[ProbeColumn.CUT_SEQ.value])

    def _failure(self, action: str, exc: Exception) -> Exception:
        return HistoryError(self._detail(action, exc))

    async def _prepare(self) -> None:
        """Файлы схемы пакета и сверка раскладки — один раз на хранилище:
        агент — отдельная программа, таблицы истории создаёт сам."""
        if self._ready:
            return

        schema_dir = files("boba.history.postgres") / "schema"
        conn = await self._dedicated()
        try:
            await self._schema.apply_files(conn, schema_dir, {})
        except PostgresError as exc:
            raise self._failure("applying the history schema files", exc) from exc
        finally:
            await conn.close()

        session_columns = [column.value for column in SessionColumn]
        history_columns = [column.value for column in HistoryColumn]
        await self._check_layouts(
            {
                HistoryTable.SESSION.value: session_columns,
                HistoryTable.HISTORY.value: history_columns,
            }
        )
        self._ready = True

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
        acquired = False
        if row is not None:
            acquired = bool(row[0])

        if not acquired:
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
                    {schema}.history
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

    def _rows_of(
        self, session: SessionId, records: Iterable[Record]
    ) -> Iterator[Mapping[str, Any]]:
        """Параметры вставки в history: по строке на запись."""
        for record in records:
            row = self._frames.split(session, record)
            yield {
                HistoryColumn.SESSION_ID.value: session.value,
                HistoryColumn.SEQ.value: row.seq,
                HistoryColumn.TYPE.value: row.type,
                HistoryColumn.RECORDED_AT.value: row.recorded_at,
                HistoryColumn.RECORD.value: Jsonb(row.record),
            }
