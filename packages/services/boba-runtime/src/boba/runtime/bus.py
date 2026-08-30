"""Шина сообщений на Postgres: сообщения в live_events, команды в live_commands,
указатель через pg_notify; LiveListener процесса читает канал на выделенном
соединении и раздаёт конверты подписчикам в порядке seq.

Ошибки:
MessageBusError — база недоступна, строка не сохранена или не прочитана; слушатель
    процесса остановлен сбоем подписчика.
MessageTooLargeError — тело сообщения больше лимита.
LockLostError — сообщение держателя публикуется без живой блокировки с этим token.
ListenerFailedError — подписчик не справился с конвертом; слушатель процесса
    останавливается, и до перезапуска шина отказывает.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema, SqlNames
from boba.identity.context import Scope, ScopeKind
from boba.identity.locks import LiveLocksColumn
from boba.messaging import (
    AnyCommand,
    AnyMessage,
    BusLimit,
    CommandEnvelope,
    CommandListener,
    Envelope,
    Listener,
    ListenerFailedError,
    LockLostError,
    LockToken,
    MessageBus,
    MessageBusError,
    MessageTooLargeError,
    Unsubscribe,
)
from boba.messaging.bus import (
    BusWatch,
    ListenerState,
    LiveChannel,
    LiveCommandsColumn,
    LiveEventsColumn,
    LiveTable,
    StateListener,
)
from boba.runtime.config import AppName, ClusterConfig

__all__ = [
    "LiveListener",
    "PgMessageBus",
    "Pointer",
    "PointerKind",
]

logger = logging.getLogger(__name__)


class PointerKind(StrEnum):
    """Что означает уведомление: в области появилось событие или команда."""

    EVENT = "event"
    COMMAND = "command"


class Pointer(BaseModel):
    """Тело уведомления pg_notify: указатель на строку таблицы, по которому подписчик
    читает сами данные.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PointerKind
    scope_kind: ScopeKind
    scope_id: UUID
    seq: int = Field(ge=1)

    @property
    def scope(self) -> Scope:
        return Scope(kind=self.scope_kind, id=str(self.scope_id))

    def render(self) -> str:
        return self.model_dump_json()

    @classmethod
    def parse(cls, raw: str) -> Pointer:
        return cls.model_validate_json(raw)


PointerHandler = Callable[[Pointer], Awaitable[None]]
ReconnectHandler = Callable[[], Awaitable[None]]


class ScopeKindCheck:
    """Check-ограничение на вид области: одно и то же у всех live-таблиц."""

    KINDS: ClassVar[tuple[str, ...]] = ("chat", "workflow", "job", "user")

    @classmethod
    def of(cls, schema: str, table: LiveTable) -> sql.Composed:
        constraint = sql.Identifier(f"{table.value}_scope_kind_check")
        kinds = sql.SQL(", ").join([sql.Literal(kind) for kind in cls.KINDS])

        return sql.SQL(
            """
            alter table {table}
                drop constraint if exists {constraint},
                add constraint {constraint} check (scope_kind in ({kinds}))
            """
        ).format(
            table=SqlNames.table(schema, table), constraint=constraint, kinds=kinds
        )


class LiveListener(BusWatch):
    """Слушает канал boba_live на выделенном autocommit-соединении, переподключается
    после обрыва и добирает пропущенное через обработчик реконнекта.
    """

    RETRY_SEC: ClassVar[float] = 1.0
    RETRY_MAX_SEC: ClassVar[float] = 10.0

    def __init__(
        self,
        cfg: PostgresConfig,
        handler: PointerHandler,
        on_reconnect: ReconnectHandler,
    ) -> None:
        self._cfg = cfg
        self._handler = handler
        self._on_reconnect = on_reconnect
        self._task: asyncio.Task[None] | None = None
        self._conn: psycopg.AsyncConnection[Any] | None = None
        self._state = ListenerState.STOPPED
        self._ready = asyncio.Event()
        self._connections = 0
        self._failure: MessageBusError | None = None
        self._watchers: list[StateListener] = []
        self._stopping = False

    @property
    def state(self) -> ListenerState:
        return self._state

    def watch(self, listener: StateListener) -> Unsubscribe:
        self._watchers.append(listener)

        def leave() -> None:
            if listener in self._watchers:
                self._watchers.remove(listener)

        return leave

    def _set_state(self, state: ListenerState) -> None:
        if state is self._state:
            return

        self._state = state
        for watcher in list(self._watchers):
            watcher(state)

    async def reconnect(self) -> None:
        """Рвёт соединение слушателя, чтобы освободить его долю очереди уведомлений;
        цикл переподключится и доберёт пропущенное.
        """
        conn = self._conn
        if conn is None:
            return

        await conn.close()

    def ensure_alive(self) -> None:
        """Отвергает обращение MessageBusError, если слушатель остановлен сбоем: без
        него шина процесса непригодна до перезапуска.
        """
        if self._failure is None:
            return

        msg = "live listener has failed and the bus is unusable"
        raise MessageBusError(msg) from self._failure

    @property
    def backend_pid(self) -> int:
        if self._conn is None:
            msg = "listener is not connected"
            raise MessageBusError(msg)

        return self._conn.info.backend_pid

    async def start(self) -> None:
        if self._task is not None:
            return

        self._stopping = False
        self._set_state(ListenerState.CONNECTING)
        self._task = asyncio.create_task(self._run(), name="live-listener")
        await self._ready.wait()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        self._task = None
        # сначала рвётся соединение: ожидание notifies() внутри psycopg не всегда
        # отзывается на отмену задачи, а закрытый сокет выводит его ошибкой
        self._stopping = True
        await self._close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except MessageBusError:
            # сбой уже учтён: он в журнале и поднимается ensure_alive при обращении
            pass

        self._set_state(ListenerState.STOPPED)

    async def wait_listening(self, timeout_sec: float) -> None:
        """Ждёт повторного подключения слушателя не дольше timeout_sec; нужен тестам
        обрыва связи.
        """
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while self._state is not ListenerState.LISTENING:
            if asyncio.get_running_loop().time() > deadline:
                msg = "listener did not reconnect in time"
                raise MessageBusError(msg)

            await asyncio.sleep(0.05)

    async def _run(self) -> None:
        delay = self.RETRY_SEC
        while True:
            try:
                await self._listen_once()
                delay = self.RETRY_SEC
            except asyncio.CancelledError:
                await self._close()
                raise
            except (psycopg.Error, PostgresError, OSError) as exc:
                if self._stopping:
                    return

                self._set_state(ListenerState.CONNECTING)
                await self._close()
                logger.warning("live listener lost the connection: %s", exc)
                self._ready.set()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_SEC)
            except ListenerFailedError as exc:
                # сбой подписчика: слушатель встаёт, шина отказывает при следующем
                # обращении, а не молчит
                self._set_state(ListenerState.FAILED)
                self._failure = exc
                await self._close()
                logger.exception("live listener stopped: %s", exc)
                self._ready.set()
                raise
            except MessageBusError as exc:
                # база ответила ошибкой при догоне или уведомление негодно: связь
                # переустанавливается, как после обрыва
                if self._stopping:
                    return

                self._set_state(ListenerState.CONNECTING)
                await self._close()
                logger.warning("live listener retries after a bus error: %s", exc)
                self._ready.set()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_SEC)
            except Exception as exc:
                # неожиданная ошибка разбора или доставки: тот же исход, что у сбоя
                # подписчика — иначе задача умерла бы молча, а шина считалась бы живой
                failure = MessageBusError(f"live listener crashed: {exc}")
                failure.__cause__ = exc
                self._set_state(ListenerState.FAILED)
                self._failure = failure
                await self._close()
                logger.exception("live listener crashed: %s", exc)
                self._ready.set()
                raise failure from exc

    async def _listen_once(self) -> None:
        conn = await AsyncPostgresPool.dedicated(self._cfg)
        self._conn = conn
        await conn.execute(
            sql.SQL("listen {channel}").format(
                channel=sql.Identifier(LiveChannel.LIVE.value)
            )
        )
        self._set_state(ListenerState.LISTENING)
        self._connections += 1
        self._ready.set()
        logger.info("live listener connected (pid %d)", conn.info.backend_pid)

        if self._connections > 1:
            await self._on_reconnect()

        async for notification in conn.notifies():
            try:
                pointer = Pointer.parse(notification.payload)
            except ValidationError as exc:
                msg = f"bad notification payload: {notification.payload!r}"
                raise MessageBusError(msg) from exc

            await self._handler(pointer)

    async def _close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return

        # соединение закрывается после обрыва: отказ закрытия уже ничего не значит
        with contextlib.suppress(psycopg.Error, OSError):
            await conn.close()


class PgMessageBus(MessageBus):
    """Шина процесса на таблицах схемы чата: публикует через пул соединений,
    принимает через LiveListener; один экземпляр на процесс.
    """

    SETUP_LOCK: ClassVar[str] = "boba-live-setup"
    """Ключ advisory-lock, под которым процессы по очереди создают live-таблицы."""

    def __init__(
        self,
        cfg: PostgresConfig,
        db_schema: str,
        instance: str,
        app: AppName,
        cluster: ClusterConfig,
    ) -> None:
        self._cfg = cfg
        self._schema = db_schema
        self._instance = instance
        self._app = app
        self._cluster = cluster
        self._pool_ref: AsyncPostgresPool | None = None
        self._listener = LiveListener(cfg, self._on_pointer, self._catch_up_all)
        self._listeners: dict[Scope, list[Listener]] = {}
        self._command_listeners: list[CommandListener] = []
        self._last_seen: dict[Scope, int] = {}
        self._scope_locks: dict[Scope, asyncio.Lock] = {}
        self._envelope = TypeAdapter(AnyMessage)
        self._command_body = TypeAdapter(AnyCommand)

    @property
    def instance(self) -> str:
        return self._instance

    @property
    def listener(self) -> LiveListener:
        return self._listener

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._cfg)

        return self._pool_ref

    def _table(self, table: LiveTable) -> sql.Identifier:
        return SqlNames.table(self._schema, table)

    async def setup(self) -> None:
        """Создаёт схему и таблицы шины (live_instances, live_events, live_commands);
        live_locks и live_payloads создают их владельцы после шины.
        """
        pool = await self._pool()

        try:
            async with pool.connection() as conn, conn.transaction():
                # процессы кластера стартуют разом: DDL по очереди под advisory-lock
                await conn.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                    {"key": self.SETUP_LOCK},
                    prepare=False,
                )
                await PostgresSchema.ensure(conn, self._schema)

                for query in self._ddl():
                    await conn.execute(query, prepare=False)
        except (psycopg.Error, PostgresError) as exc:
            msg = "message bus: setup failed"
            raise MessageBusError(msg) from exc

        logger.info("message bus ready: instance %s", self._instance)

    def _ddl(self) -> tuple[sql.Composed, ...]:
        instances = self._table(LiveTable.INSTANCES)
        return (
            sql.SQL(
                """
                create unlogged table if not exists {instances} (
                    instance_id  text primary key,
                    app          text not null check (app in ('chainlit', 'studio')),
                    host         text not null,
                    started_at   timestamptz not null default now(),
                    heartbeat_at timestamptz not null default now()
                )
                """
            ).format(instances=instances),
            sql.SQL(
                """
                create unlogged table if not exists {events} (
                    scope_kind text not null
                        check (scope_kind in ('chat', 'workflow', 'job', 'user')),
                    scope_id   uuid not null,
                    seq        bigint not null,
                    kind       text not null,
                    origin     text not null,
                    body       jsonb not null,
                    at         timestamptz not null default now(),
                    primary key (scope_kind, scope_id, seq)
                )
                """
            ).format(events=self._table(LiveTable.EVENTS)),
            sql.SQL(
                """
                create unlogged table if not exists {commands} (
                    id          bigint generated always as identity primary key,
                    scope_kind  text not null
                        check (scope_kind in ('chat', 'workflow', 'job', 'user')),
                    scope_id    uuid not null,
                    action      text not null check (action in ('stop')),
                    body        jsonb not null,
                    by_instance text not null,
                    at          timestamptz not null default now(),
                    taken_by    text,
                    taken_at    timestamptz
                )
                """
            ).format(commands=self._table(LiveTable.COMMANDS)),
            sql.SQL(
                """
                alter table {commands}
                    drop constraint if exists live_commands_by_instance_fkey,
                    drop constraint if exists live_commands_taken_by_fkey
                """
            ).format(commands=self._table(LiveTable.COMMANDS)),
            sql.SQL(
                """
                create index if not exists idx_live_commands_scope
                on {commands} (scope_kind, scope_id)
                """
            ).format(commands=self._table(LiveTable.COMMANDS)),
            *self._scope_kind_checks(),
        )

    def _scope_kind_checks(self) -> tuple[sql.Composed, ...]:
        """Перечень видов областей в check-ограничениях уже созданных таблиц."""
        checks: list[sql.Composed] = []
        for table in (LiveTable.EVENTS, LiveTable.COMMANDS):
            checks.append(ScopeKindCheck.of(self._schema, table))

        return tuple(checks)

    async def start(self) -> None:
        await self._listener.start()

    async def stop(self) -> None:
        await self._listener.stop()

    @staticmethod
    def _scope_id(scope: Scope) -> UUID:
        try:
            return UUID(scope.id)
        except ValueError as exc:
            msg = f"scope id is not a uuid: {scope.id!r}"
            raise MessageBusError(msg) from exc

    async def publish(self, scope: Scope, message: AnyMessage, token: LockToken) -> int:
        self._listener.ensure_alive()
        size = Envelope.body_size(message)
        if size > BusLimit.BODY_MAX_BYTES:
            msg = f"message {message.kind} of {size} bytes exceeds the bus limit"
            raise MessageTooLargeError(msg)

        scope_id = self._scope_id(scope)
        pool = await self._pool()

        try:
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                    {"key": scope.render()},
                    prepare=False,
                )
                if message.kind.requires_lock:
                    await self._fence(conn, scope, scope_id, token)

                cur = await conn.execute(
                    sql.SQL(
                        """
                        insert into {events}
                            ({scope_kind}, {scope_id}, {seq}, {kind}, {origin}, {body})
                        select
                            %(scope_kind)s,
                            %(scope_id)s,
                            coalesce(max({seq}), 0) + 1,
                            %(kind)s,
                            %(origin)s,
                            %(body)s
                        from {events}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                        returning {seq}
                        """
                    ).format(
                        events=self._table(LiveTable.EVENTS),
                        scope_kind=SqlNames.ident(LiveEventsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveEventsColumn.SCOPE_ID),
                        seq=SqlNames.ident(LiveEventsColumn.SEQ),
                        kind=SqlNames.ident(LiveEventsColumn.KIND),
                        origin=SqlNames.ident(LiveEventsColumn.ORIGIN),
                        body=SqlNames.ident(LiveEventsColumn.BODY),
                    ),
                    {
                        "scope_kind": scope.kind.value,
                        "scope_id": scope_id,
                        "kind": message.kind.value,
                        "origin": self._instance,
                        "body": Jsonb(message.model_dump(mode="json")),
                    },
                    prepare=False,
                )
                row = await cur.fetchone()
                if row is None:
                    msg = "message bus: insert returned no seq"
                    raise MessageBusError(msg)

                seq = int(row[0])
                pointer = Pointer(
                    kind=PointerKind.EVENT,
                    scope_kind=scope.kind,
                    scope_id=scope_id,
                    seq=seq,
                )
                await conn.execute(
                    "select pg_notify(%(channel)s, %(payload)s)",
                    {"channel": LiveChannel.LIVE.value, "payload": pointer.render()},
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: publish to {scope.render()} failed"
            raise MessageBusError(msg) from exc

        return seq

    async def _fence(
        self,
        conn: psycopg.AsyncConnection[Any],
        scope: Scope,
        scope_id: UUID,
        token: LockToken,
    ) -> None:
        """Проверяет, что блокировка держателя жива и token совпадает; иначе
        публикация отвергается LockLostError.
        """
        cur = await conn.execute(
            sql.SQL(
                """
                select
                    1
                from {locks}
                where 1=1
                    and {scope_kind} = %(scope_kind)s
                    and {scope_id} = %(scope_id)s
                    and {token} = %(token)s
                    and {heartbeat} + make_interval(secs => {ttl}) >= now()
                """
            ).format(
                locks=self._table(LiveTable.LOCKS),
                scope_kind=SqlNames.ident(LiveLocksColumn.SCOPE_KIND),
                scope_id=SqlNames.ident(LiveLocksColumn.SCOPE_ID),
                token=SqlNames.ident(LiveLocksColumn.TOKEN),
                heartbeat=SqlNames.ident(LiveLocksColumn.HEARTBEAT_AT),
                ttl=SqlNames.ident(LiveLocksColumn.TTL_SEC),
            ),
            {
                "scope_kind": scope.kind.value,
                "scope_id": scope_id,
                "token": token.value,
            },
            prepare=False,
        )
        row = await cur.fetchone()
        if row is None:
            msg = f"lock of {scope.render()} is lost: publish refused"
            raise LockLostError(msg)

    async def command(self, scope: Scope, command: AnyCommand) -> int:
        self._listener.ensure_alive()
        scope_id = self._scope_id(scope)
        pool = await self._pool()

        try:
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    sql.SQL(
                        """
                        insert into {commands} (
                            {scope_kind},
                            {scope_id},
                            {action},
                            {body},
                            {by_instance}
                        )
                        values (
                            %(scope_kind)s,
                            %(scope_id)s,
                            %(action)s,
                            %(body)s,
                            %(by_instance)s
                        )
                        returning {id}
                        """
                    ).format(
                        commands=self._table(LiveTable.COMMANDS),
                        scope_kind=SqlNames.ident(LiveCommandsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveCommandsColumn.SCOPE_ID),
                        action=SqlNames.ident(LiveCommandsColumn.ACTION),
                        body=SqlNames.ident(LiveCommandsColumn.BODY),
                        by_instance=SqlNames.ident(LiveCommandsColumn.BY_INSTANCE),
                        id=SqlNames.ident(LiveCommandsColumn.ID),
                    ),
                    {
                        "scope_kind": scope.kind.value,
                        "scope_id": scope_id,
                        "action": command.kind.value,
                        "body": Jsonb(command.model_dump(mode="json")),
                        "by_instance": self._instance,
                    },
                    prepare=False,
                )
                row = await cur.fetchone()
                if row is None:
                    msg = "message bus: command insert returned no id"
                    raise MessageBusError(msg)

                command_id = int(row[0])
                pointer = Pointer(
                    kind=PointerKind.COMMAND,
                    scope_kind=scope.kind,
                    scope_id=scope_id,
                    seq=command_id,
                )
                await conn.execute(
                    "select pg_notify(%(channel)s, %(payload)s)",
                    {"channel": LiveChannel.LIVE.value, "payload": pointer.render()},
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: command to {scope.render()} failed"
            raise MessageBusError(msg) from exc

        return command_id

    def subscribe(self, scope: Scope, listener: Listener) -> Unsubscribe:
        self._listener.ensure_alive()
        listeners = self._listeners.setdefault(scope, [])
        listeners.append(listener)
        logger.info(
            "subscribed %s on bus %x (%d listeners)",
            scope.render(),
            id(self),
            len(listeners),
        )

        def leave() -> None:
            current = self._listeners.get(scope)
            if current is None:
                return

            if listener in current:
                current.remove(listener)

            if not current:
                del self._listeners[scope]
                self._last_seen.pop(scope, None)
                self._scope_locks.pop(scope, None)

        return leave

    def subscribe_commands(self, listener: CommandListener) -> Unsubscribe:
        self._command_listeners.append(listener)

        def leave() -> None:
            if listener in self._command_listeners:
                self._command_listeners.remove(listener)

        return leave

    async def replay(self, scope: Scope, after_seq: int) -> Sequence[Envelope]:
        rows = await self._rows_after(scope, after_seq)
        return [self._to_envelope(scope, row) for row in rows]

    async def take(self, scope: Scope, command_id: int, instance: str) -> bool:
        pool = await self._pool()

        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        update {commands}
                        set {taken_by} = %(instance)s,
                            {taken_at} = now()
                        where 1=1
                            and {id} = %(id)s
                            and {taken_by} is null
                        """
                    ).format(
                        commands=self._table(LiveTable.COMMANDS),
                        taken_by=SqlNames.ident(LiveCommandsColumn.TAKEN_BY),
                        taken_at=SqlNames.ident(LiveCommandsColumn.TAKEN_AT),
                        id=SqlNames.ident(LiveCommandsColumn.ID),
                    ),
                    {"instance": instance, "id": command_id},
                    prepare=False,
                )
                return cur.rowcount == 1
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: take of command {command_id} failed"
            raise MessageBusError(msg) from exc

    async def purge(self, scope: Scope) -> int:
        scope_id = self._scope_id(scope)
        pool = await self._pool()
        params = {"scope_kind": scope.kind.value, "scope_id": scope_id}

        try:
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    sql.SQL(
                        """
                        delete from {events}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        events=self._table(LiveTable.EVENTS),
                        scope_kind=SqlNames.ident(LiveEventsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveEventsColumn.SCOPE_ID),
                    ),
                    params,
                    prepare=False,
                )
                removed = cur.rowcount
                await conn.execute(
                    sql.SQL(
                        """
                        delete from {commands}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        commands=self._table(LiveTable.COMMANDS),
                        scope_kind=SqlNames.ident(LiveCommandsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveCommandsColumn.SCOPE_ID),
                    ),
                    params,
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: purge of {scope.render()} failed"
            raise MessageBusError(msg) from exc

        self._last_seen.pop(scope, None)
        return removed

    async def queue_usage(self) -> float:
        """Возвращает долю занятой очереди уведомлений Postgres: 0 — пусто, 1 —
        полна.
        """
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute("select pg_notification_queue_usage()", prepare=False)
                row = await cur.fetchone()
        except (psycopg.Error, PostgresError) as exc:
            msg = "message bus: queue usage is not available"
            raise MessageBusError(msg) from exc

        if row is None:
            return 0.0

        return float(row[0])

    async def purge_idle(self, max_age_sec: int) -> int:
        """Удаляет события и команды областей, в которых ничего не происходило дольше
        max_age_sec; возвращает число удалённых событий.
        """
        pool = await self._pool()
        try:
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    sql.SQL(
                        """
                        delete from {events} e
                        where not exists (
                            select 1 from {events} f
                            where 1=1
                              and f.{scope_kind} = e.{scope_kind}
                              and f.{scope_id} = e.{scope_id}
                              and f.{at} + make_interval(secs => %(age)s) >= now()
                         )
                        """
                    ).format(
                        events=self._table(LiveTable.EVENTS),
                        scope_kind=SqlNames.ident(LiveEventsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveEventsColumn.SCOPE_ID),
                        at=SqlNames.ident(LiveEventsColumn.AT),
                    ),
                    {"age": max_age_sec},
                    prepare=False,
                )
                removed = cur.rowcount
                await conn.execute(
                    sql.SQL(
                        """
                        delete from {commands}
                        where 1=1
                        and {at} + make_interval(secs => %(age)s) < now()
                        """
                    ).format(
                        commands=self._table(LiveTable.COMMANDS),
                        at=SqlNames.ident(LiveCommandsColumn.AT),
                    ),
                    {"age": max_age_sec},
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = "message bus: purge of idle scopes failed"
            raise MessageBusError(msg) from exc

        return removed

    async def _rows_after(self, scope: Scope, after_seq: int) -> Sequence[DictRow]:
        scope_id = self._scope_id(scope)
        pool = await self._pool()

        try:
            async with pool.dict_cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        select
                            {seq},
                            {origin},
                            {body},
                            {at}
                        from {events}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                            and {seq} > %(after)s
                        order by {seq}
                        """
                    ).format(
                        events=self._table(LiveTable.EVENTS),
                        scope_kind=SqlNames.ident(LiveEventsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LiveEventsColumn.SCOPE_ID),
                        seq=SqlNames.ident(LiveEventsColumn.SEQ),
                        origin=SqlNames.ident(LiveEventsColumn.ORIGIN),
                        body=SqlNames.ident(LiveEventsColumn.BODY),
                        at=SqlNames.ident(LiveEventsColumn.AT),
                    ),
                    {
                        "scope_kind": scope.kind.value,
                        "scope_id": scope_id,
                        "after": after_seq,
                    },
                    prepare=False,
                )
                return await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: read of {scope.render()} failed"
            raise MessageBusError(msg) from exc

    def _to_envelope(self, scope: Scope, row: DictRow) -> Envelope:
        message = self._envelope.validate_python(row[LiveEventsColumn.BODY.value])
        at: datetime = row[LiveEventsColumn.AT.value]
        return Envelope(
            scope=scope,
            seq=int(row[LiveEventsColumn.SEQ.value]),
            at=at,
            origin=str(row[LiveEventsColumn.ORIGIN.value]),
            message=message,
        )

    async def _on_pointer(self, pointer: Pointer) -> None:
        if pointer.kind is PointerKind.COMMAND:
            await self._deliver_command(pointer)
            return

        scope = pointer.scope
        if scope not in self._listeners:
            logger.debug(
                "pointer %s seq %d: no listeners here", scope.render(), pointer.seq
            )
            return

        lock = self._scope_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            await self._deliver_events(scope, pointer.seq)

    async def _deliver_events(self, scope: Scope, seen_seq: int) -> None:
        after = self._last_seen.get(scope)
        if after is None:
            after = seen_seq - 1

        rows = await self._rows_after(scope, after)
        for row in rows:
            envelope = self._to_envelope(scope, row)
            self._last_seen[scope] = envelope.seq

            failures: list[BaseException] = []
            for listener in list(self._listeners.get(scope, ())):
                try:
                    await listener(envelope)
                except Exception as exc:
                    failures.append(exc)

            if failures:
                what = f"{scope.render()}: listener failed on seq {envelope.seq}"
                raise ListenerFailedError(what, failures)

    async def _catch_up_all(self) -> None:
        for scope, after in list(self._last_seen.items()):
            if scope not in self._listeners:
                continue

            lock = self._scope_locks.setdefault(scope, asyncio.Lock())
            async with lock:
                await self._deliver_events(scope, after + 1)

    async def _deliver_command(self, pointer: Pointer) -> None:
        if not self._command_listeners:
            return

        pool = await self._pool()
        try:
            async with pool.dict_cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        select {body}, {at} from {commands} where {id} = %(id)s
                        """
                    ).format(
                        commands=self._table(LiveTable.COMMANDS),
                        body=SqlNames.ident(LiveCommandsColumn.BODY),
                        at=SqlNames.ident(LiveCommandsColumn.AT),
                        id=SqlNames.ident(LiveCommandsColumn.ID),
                    ),
                    {"id": pointer.seq},
                    prepare=False,
                )
                row = await cur.fetchone()
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: read of command {pointer.seq} failed"
            raise MessageBusError(msg) from exc

        if row is None:
            return

        command = self._command_body.validate_python(row[LiveCommandsColumn.BODY.value])
        at: datetime = row[LiveCommandsColumn.AT.value]
        envelope = CommandEnvelope(
            scope=pointer.scope, command_id=pointer.seq, at=at, command=command
        )
        failures: list[BaseException] = []
        for listener in list(self._command_listeners):
            try:
                await listener(envelope)
            except Exception as exc:
                failures.append(exc)

        if failures:
            what = f"{pointer.scope.render()}: command listener failed on {pointer.seq}"
            raise ListenerFailedError(what, failures)
