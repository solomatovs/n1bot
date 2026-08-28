"""Шина сообщений на Postgres: сообщения в live_events, команды в live_commands,
указатель через pg_notify; LiveListener процесса читает канал на выделенном
соединении и раздаёт конверты подписчикам в порядке seq.

Ошибки:
MessageBusError — база недоступна, строка не сохранена или не прочитана; слушатель
    процесса остановлен сбоем подписчика или негодным уведомлением.
MessageTooLargeError — тело сообщения больше лимита.
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
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.identity.context import Scope, ScopeKind
from boba.messaging import (
    AnyCommand,
    AnyMessage,
    BusLimit,
    CommandEnvelope,
    CommandListener,
    Envelope,
    Listener,
    ListenerFailedError,
    LockToken,
    MessageBus,
    MessageBusError,
    MessageTooLargeError,
    Unsubscribe,
)
from boba.runtime.config import AppName, ClusterConfig
from boba.runtime.tables import (
    ChatTable,
    LiveChannel,
    LiveCommandsColumn,
    LiveEventsColumn,
    LiveInstancesColumn,
)

__all__ = ["ListenerState", "LiveListener", "PgMessageBus", "Pointer", "PointerKind"]

logger = logging.getLogger(__name__)


class PointerKind(StrEnum):
    """Что означает уведомление: в области появилось событие или команда."""

    EVENT = "event"
    COMMAND = "command"


class Pointer(BaseModel):
    """Тело уведомления pg_notify: только указатель на строку таблицы, сами данные
    подписчик читает из неё.
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


class ListenerState(StrEnum):
    """Состояние слушателя процесса; страница показывает его лампочкой у сокета."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    LISTENING = "listening"
    FAILED = "failed"


PointerHandler = Callable[[Pointer], Awaitable[None]]
ReconnectHandler = Callable[[], Awaitable[None]]


class LiveListener:
    """Слушатель канала boba_live на выделенном autocommit-соединении с
    переподключением и догоном пропущенного.
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

    @property
    def state(self) -> ListenerState:
        return self._state

    def ensure_alive(self) -> None:
        """Отказывает MessageBusError, если слушатель остановлен сбоем: без него шина
        процесса непригодна до перезапуска.
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

        self._state = ListenerState.CONNECTING
        self._task = asyncio.create_task(self._run(), name="live-listener")
        await self._ready.wait()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except MessageBusError:
            # сбой уже учтён: он в журнале и поднимается ensure_alive при обращении
            pass

        self._state = ListenerState.STOPPED

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
                self._state = ListenerState.CONNECTING
                await self._close()
                logger.warning("live listener lost the connection: %s", exc)
                self._ready.set()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_SEC)
            except MessageBusError as exc:
                # сбой подписчика или негодное уведомление: слушатель встаёт, шина
                # отказывает при следующем обращении, а не молчит
                self._state = ListenerState.FAILED
                self._failure = exc
                await self._close()
                logger.exception("live listener stopped: %s", exc)
                self._ready.set()
                raise

    async def _listen_once(self) -> None:
        conn = await AsyncPostgresPool.dedicated(self._cfg)
        self._conn = conn
        await conn.execute(
            sql.SQL("listen {channel}").format(
                channel=sql.Identifier(LiveChannel.LIVE.value)
            )
        )
        self._state = ListenerState.LISTENING
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
    """Шина процесса на таблицах схемы чата: публикует через пул, принимает через
    LiveListener; один экземпляр на процесс.
    """

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

    def _table(self, table: ChatTable) -> sql.Identifier:
        return table.under(self._schema)

    async def setup(self) -> None:
        """Создаёт таблицы шины и регистрирует инстанс в live_instances; зовётся на
        старте процесса.
        """
        pool = await self._pool()

        try:
            async with pool.connection() as conn:
                try:
                    await conn.execute(
                        sql.SQL("create schema if not exists {schema}").format(
                            schema=sql.Identifier(self._schema)
                        ),
                        prepare=False,
                    )
                except InsufficientPrivilege:
                    logger.info("no permission for create schema %r", self._schema)

                for query in self._ddl():
                    await conn.execute(query, prepare=False)

                await conn.execute(
                    sql.SQL(
                        """
                        insert into {instances} ({id}, {app}, {host})
                        values (%(id)s, %(app)s, %(host)s)
                        on conflict ({id}) do update
                        set {app} = excluded.{app}, {host} = excluded.{host},
                            {started} = now(), {heartbeat} = now()
                        """
                    ).format(
                        instances=self._table(ChatTable.LIVE_INSTANCES),
                        id=LiveInstancesColumn.INSTANCE_ID.ident(),
                        app=LiveInstancesColumn.APP.ident(),
                        host=LiveInstancesColumn.HOST.ident(),
                        started=LiveInstancesColumn.STARTED_AT.ident(),
                        heartbeat=LiveInstancesColumn.HEARTBEAT_AT.ident(),
                    ),
                    {
                        "id": self._instance,
                        "app": self._app.value,
                        "host": self._cluster.host,
                    },
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = "message bus: setup failed"
            raise MessageBusError(msg) from exc

        logger.info("message bus ready: instance %s", self._instance)

    def _ddl(self) -> tuple[sql.Composed, ...]:
        instances = self._table(ChatTable.LIVE_INSTANCES)
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
                        check (scope_kind in ('chat', 'workflow', 'job')),
                    scope_id   uuid not null,
                    seq        bigint not null,
                    kind       text not null,
                    origin     text not null,
                    body       jsonb not null,
                    at         timestamptz not null default now(),
                    primary key (scope_kind, scope_id, seq)
                )
                """
            ).format(events=self._table(ChatTable.LIVE_EVENTS)),
            sql.SQL(
                """
                create unlogged table if not exists {commands} (
                    id          bigint generated always as identity primary key,
                    scope_kind  text not null
                        check (scope_kind in ('chat', 'workflow', 'job')),
                    scope_id    uuid not null,
                    action      text not null check (action in ('stop')),
                    body        jsonb not null,
                    by_instance text not null references {instances} (instance_id),
                    at          timestamptz not null default now(),
                    taken_by    text references {instances} (instance_id),
                    taken_at    timestamptz
                )
                """
            ).format(
                commands=self._table(ChatTable.LIVE_COMMANDS), instances=instances
            ),
            sql.SQL(
                """
                create index if not exists idx_live_commands_scope
                on {commands} (scope_kind, scope_id)
                """
            ).format(commands=self._table(ChatTable.LIVE_COMMANDS)),
        )

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
                cur = await conn.execute(
                    sql.SQL(
                        """
                        insert into {events}
                            ({scope_kind}, {scope_id}, {seq}, {kind}, {origin}, {body})
                        select %(scope_kind)s, %(scope_id)s,
                               coalesce(max({seq}), 0) + 1,
                               %(kind)s, %(origin)s, %(body)s
                          from {events}
                         where {scope_kind} = %(scope_kind)s
                           and {scope_id} = %(scope_id)s
                        returning {seq}
                        """
                    ).format(
                        events=self._table(ChatTable.LIVE_EVENTS),
                        scope_kind=LiveEventsColumn.SCOPE_KIND.ident(),
                        scope_id=LiveEventsColumn.SCOPE_ID.ident(),
                        seq=LiveEventsColumn.SEQ.ident(),
                        kind=LiveEventsColumn.KIND.ident(),
                        origin=LiveEventsColumn.ORIGIN.ident(),
                        body=LiveEventsColumn.BODY.ident(),
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

    async def command(self, scope: Scope, command: AnyCommand) -> int:
        self._listener.ensure_alive()
        scope_id = self._scope_id(scope)
        pool = await self._pool()

        try:
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    sql.SQL(
                        """
                        insert into {commands}
                            ({scope_kind}, {scope_id}, {action}, {body}, {by_instance})
                        values (%(scope_kind)s, %(scope_id)s, %(action)s, %(body)s,
                                %(by_instance)s)
                        returning {id}
                        """
                    ).format(
                        commands=self._table(ChatTable.LIVE_COMMANDS),
                        scope_kind=LiveCommandsColumn.SCOPE_KIND.ident(),
                        scope_id=LiveCommandsColumn.SCOPE_ID.ident(),
                        action=LiveCommandsColumn.ACTION.ident(),
                        body=LiveCommandsColumn.BODY.ident(),
                        by_instance=LiveCommandsColumn.BY_INSTANCE.ident(),
                        id=LiveCommandsColumn.ID.ident(),
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
                           set {taken_by} = %(instance)s, {taken_at} = now()
                         where {id} = %(id)s and {taken_by} is null
                        """
                    ).format(
                        commands=self._table(ChatTable.LIVE_COMMANDS),
                        taken_by=LiveCommandsColumn.TAKEN_BY.ident(),
                        taken_at=LiveCommandsColumn.TAKEN_AT.ident(),
                        id=LiveCommandsColumn.ID.ident(),
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
                         where {scope_kind} = %(scope_kind)s
                           and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        events=self._table(ChatTable.LIVE_EVENTS),
                        scope_kind=LiveEventsColumn.SCOPE_KIND.ident(),
                        scope_id=LiveEventsColumn.SCOPE_ID.ident(),
                    ),
                    params,
                    prepare=False,
                )
                removed = cur.rowcount
                await conn.execute(
                    sql.SQL(
                        """
                        delete from {commands}
                         where {scope_kind} = %(scope_kind)s
                           and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        commands=self._table(ChatTable.LIVE_COMMANDS),
                        scope_kind=LiveCommandsColumn.SCOPE_KIND.ident(),
                        scope_id=LiveCommandsColumn.SCOPE_ID.ident(),
                    ),
                    params,
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"message bus: purge of {scope.render()} failed"
            raise MessageBusError(msg) from exc

        self._last_seen.pop(scope, None)
        return removed

    async def _rows_after(self, scope: Scope, after_seq: int) -> Sequence[DictRow]:
        scope_id = self._scope_id(scope)
        pool = await self._pool()

        try:
            async with pool.dict_cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        select {seq}, {origin}, {body}, {at}
                          from {events}
                         where {scope_kind} = %(scope_kind)s
                           and {scope_id} = %(scope_id)s
                           and {seq} > %(after)s
                         order by {seq}
                        """
                    ).format(
                        events=self._table(ChatTable.LIVE_EVENTS),
                        scope_kind=LiveEventsColumn.SCOPE_KIND.ident(),
                        scope_id=LiveEventsColumn.SCOPE_ID.ident(),
                        seq=LiveEventsColumn.SEQ.ident(),
                        origin=LiveEventsColumn.ORIGIN.ident(),
                        body=LiveEventsColumn.BODY.ident(),
                        at=LiveEventsColumn.AT.ident(),
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
                        commands=self._table(ChatTable.LIVE_COMMANDS),
                        body=LiveCommandsColumn.BODY.ident(),
                        at=LiveCommandsColumn.AT.ident(),
                        id=LiveCommandsColumn.ID.ident(),
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
