"""Держит блокировки областей в таблице live_locks и следит за их жизнью: PgLiveLocks
захватывает, подтверждает и снимает блокировки, LockReaper убирает протухшие.

Ошибки:
LockBusyError — область занята живым держателем.
LockStoreError — база недоступна или запрос не выполнен.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

from boba.db.postgres import Cursor, PgQuery, PostgresPool, PostgresTable
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import Scope, ScopeKind
from boba.identity.locks import (
    LiveLock,
    LiveLocks,
    LiveLocksColumn,
    LockBusy,
    LockBusyError,
    LockMode,
    LockPurpose,
    LockToken,
    StaleLock,
)
from boba.messaging import MessageBusError
from boba.messaging.bus import LiveInstancesColumn, LiveTable
from boba.runtime.bus import ScopeKindCheck
from boba.runtime.config import AppName, ClusterConfig
from boba.runtime.payloads import PayloadStoreError

__all__ = ["LockReaper", "LockStoreError", "PgLiveLocks"]

logger = logging.getLogger(__name__)


class LockStoreError(Exception):
    """База блокировок недоступна или запрос не выполнен."""


class PgLiveLocks(PostgresTable, LiveLocks):
    """Захватывает, подтверждает и снимает блокировки в live_locks; протухание
    считается по часам Postgres, чтобы расхождение часов узлов не влияло.
    """

    LABEL: ClassVar[str] = "live locks"

    def __init__(  # noqa: PLR0913 — блокировки собираются из подключения и имён кластера
        self,
        cfg: PostgresConfig,
        db_schema: str,
        instance: str,
        app: AppName,
        cluster: ClusterConfig,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(cfg, db_schema, pool)
        self._instance = instance
        self._app = app
        self._cluster = cluster
        self._scope_kind_check = ScopeKindCheck(db_schema)

    @property
    def instance(self) -> str:
        return self._instance

    def _failure(self, action: str, exc: Exception) -> Exception:
        return LockStoreError(self._detail(action, exc))

    def _scope_id(self, scope: Scope) -> UUID:
        try:
            return scope.uuid()
        except ValueError as exc:
            raise LockStoreError(f"live locks: {exc}") from exc

    async def acquire(
        self, scope: Scope, mode: LockMode, purpose: LockPurpose, user_id: UUID
    ) -> LiveLock:
        scope_id = self._scope_id(scope)
        token = LockToken.local()
        expire = (
            self._query()
            .add(
                """
                delete from {schema}.live_locks
                where 1=1
                    and scope_kind = %(scope_kind)s
                    and scope_id = %(scope_id)s
                    and heartbeat_at + make_interval(secs => ttl_sec) < now()
                """,
                scope_kind=scope.kind.value,
                scope_id=scope_id,
            )
            .build()
        )
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.live_locks (
                    scope_kind,
                    scope_id,
                    mode,
                    holder,
                    token,
                    purpose,
                    user_id,
                    ttl_sec
                )
                values (
                    %(scope_kind)s,
                    %(scope_id)s,
                    %(mode)s,
                    %(holder)s,
                    %(token)s,
                    %(purpose)s,
                    %(user_id)s,
                    %(ttl)s
                )
                """,
                scope_kind=scope.kind.value,
                scope_id=scope_id,
                mode=mode.value,
                holder=self._instance,
                token=token.value,
                purpose=purpose.value,
                user_id=user_id,
                ttl=self._cluster.lock_ttl_sec,
            )
            .build()
        )

        action = (
            f"{mode.value} acquire of {scope.render()} for {purpose.value} "
            f"by {self._instance}"
        )
        async with self._transaction(action) as cur:
            await self._advisory_lock(cur, scope.render())
            await cur.execute(expire.text, expire.params)
            holders = await self._holders(cur, scope)
            for busy in holders:
                if mode is LockMode.EXCLUSIVE:
                    raise LockBusyError(scope, busy)

                if busy.mode is LockMode.EXCLUSIVE:
                    raise LockBusyError(scope, busy)

            await cur.execute(insert.text, insert.params)

        logger.info(
            "lock acquired: %s %s %s by %s",
            scope.render(),
            mode.value,
            purpose.value,
            self._instance,
        )
        return LiveLock(
            scope=scope,
            mode=mode,
            purpose=purpose,
            holder=self._instance,
            token=token,
            ttl_sec=self._cluster.lock_ttl_sec,
        )

    async def _holders(self, cur: Cursor, scope: Scope) -> Sequence[LockBusy]:
        query = (
            self._query()
            .add(
                """
                select
                    holder,
                    mode,
                    purpose,
                    extract(epoch from now() - heartbeat_at)::int as silent
                from
                    {schema}.live_locks
                where 1=1
                   and scope_kind = %(scope_kind)s
                   and scope_id = %(scope_id)s
                   and heartbeat_at + make_interval(secs => ttl_sec) >= now()
                """,
                scope_kind=scope.kind.value,
                scope_id=self._scope_id(scope),
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        rows = await cur.fetchall()

        found: list[LockBusy] = []
        for row in rows:
            found.append(
                LockBusy(
                    holder=str(row[LiveLocksColumn.HOLDER.value]),
                    mode=LockMode(str(row[LiveLocksColumn.MODE.value])),
                    purpose=LockPurpose(str(row[LiveLocksColumn.PURPOSE.value])),
                    silent_sec=max(int(row["silent"]), 0),
                )
            )

        return found

    async def holders_of(self, scope: Scope) -> Sequence[LockBusy]:
        async with self._transaction(f"reading holders of {scope.render()}") as cur:
            return await self._holders(cur, scope)

    async def heartbeat(self, token: LockToken) -> bool:
        query = (
            self._query()
            .add(
                """
                update {schema}.live_locks
                set heartbeat_at = now()
                where 1=1
                   and token = %(token)s
                   and heartbeat_at + make_interval(secs => ttl_sec) >= now()
                """,
                token=token.value,
            )
            .build()
        )
        touched = await self._execute(query, f"heartbeat of token {token.value}")

        return touched == 1

    async def setup(self) -> None:
        """Создаёт live_locks; live_instances, на которую она ссылается, создаёт
        шина, поэтому шина поднимается первой.
        """
        ddl: tuple[PgQuery, ...] = (
            self._query()
            .add(
                """
                create unlogged table if not exists {schema}.live_locks (
                    scope_kind   text not null,
                    scope_id     uuid not null,
                    mode         text not null
                        check (mode in ('exclusive', 'shared')),
                    holder       text not null
                        references {schema}.live_instances (instance_id)
                        on delete cascade,
                    token        uuid not null,
                    purpose      text not null
                        check (purpose in ('turn', 'run', 'tool_call', 'cleanup')),
                    user_id      uuid not null,
                    acquired_at  timestamptz not null default now(),
                    heartbeat_at timestamptz not null default now(),
                    ttl_sec      integer not null check (ttl_sec > 0),
                    primary key (scope_kind, scope_id, token)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_live_locks_scope
                on {schema}.live_locks (scope_kind, scope_id)
                """
            )
            .build(),
            self._scope_kind_check.of(LiveTable.LOCKS),
        )

        await self._apply_ddl(ddl)

    async def register_instance(self) -> None:
        """Записывает инстанс в live_instances или подтверждает его жизнь: строка
        заводится заново, если Postgres перезапустился и unlogged-таблица опустела.
        """
        query = (
            self._query()
            .add(
                """
                insert into {schema}.live_instances (instance_id, app, host)
                values (%(id)s, %(app)s, %(host)s)
                on conflict (instance_id)
                do update set
                    app = excluded.app,
                    host = excluded.host,
                    heartbeat_at = now()
                """,
                id=self._instance,
                app=self._app.value,
                host=self._cluster.host,
            )
            .build()
        )
        action = (
            f"registration of instance {self._instance} "
            f"({self._app.value} on {self._cluster.host}) in live_instances"
        )

        await self._execute(query, action)

    async def release(self, token: LockToken) -> None:
        query = (
            self._query()
            .add(
                "delete from {schema}.live_locks where token = %(token)s",
                token=token.value,
            )
            .build()
        )

        await self._execute(query, f"release of token {token.value}")

    async def release_all(self, holder: str) -> int:
        query = (
            self._query()
            .add(
                "delete from {schema}.live_locks where holder = %(holder)s",
                holder=holder,
            )
            .build()
        )

        return await self._execute(query, f"release of all locks of {holder}")

    async def reap(self) -> Sequence[StaleLock]:
        query = (
            self._query()
            .add(
                """
                delete from {schema}.live_locks
                where
                    heartbeat_at + make_interval(secs => ttl_sec) < now()
                returning
                    scope_kind,
                    scope_id,
                    holder,
                    purpose
                """
            )
            .build()
        )
        rows = await self._rows(query, "reap of expired locks in live_locks")

        stale: list[StaleLock] = []
        for row in rows:
            stale.append(self._stale(row))

        return stale

    def _stale(self, row: Mapping[str, Any]) -> StaleLock:
        scope = Scope(
            kind=ScopeKind(str(row[LiveLocksColumn.SCOPE_KIND.value])),
            id=str(row[LiveLocksColumn.SCOPE_ID.value]),
        )
        return StaleLock(
            scope=scope,
            holder=str(row[LiveLocksColumn.HOLDER.value]),
            purpose=LockPurpose(str(row[LiveLocksColumn.PURPOSE.value])),
        )

    async def reap_instances(self) -> Sequence[str]:
        """Удаляет инстансы, не подтверждавшие жизнь дольше ttl; их блокировки уходят
        каскадом. Возвращает имена удалённых.
        """
        query = (
            self._query()
            .add(
                """
                delete from {schema}.live_instances
                where
                    heartbeat_at + make_interval(secs => %(ttl)s) < now()
                returning
                    instance_id
                """,
                ttl=self._cluster.lock_ttl_sec,
            )
            .build()
        )
        rows = await self._rows(
            query, f"reap of instances silent for over {self._cluster.lock_ttl_sec}s"
        )

        dead: list[str] = []
        for row in rows:
            dead.append(str(row[LiveInstancesColumn.INSTANCE_ID.value]))

        return dead


StaleHandler = Callable[[Sequence[StaleLock]], Awaitable[None]]
SweepHandler = Callable[[], Awaitable[None]]


class LockReaper:
    """Периодически подтверждает жизнь своего инстанса, снимает протухшие блокировки
    и мёртвые инстансы и отдаёт снятые блокировки обработчику, который закрывает
    их ходы и запуски.
    """

    NAME: ClassVar[str] = "lock-reaper"

    def __init__(
        self,
        locks: PgLiveLocks,
        period_sec: float,
        on_stale: StaleHandler,
        on_sweep: SweepHandler,
    ) -> None:
        self._locks = locks
        self._period_sec = period_sec
        self._on_stale = on_stale
        self._on_sweep = on_sweep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=self.NAME)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def sweep(self) -> Sequence[StaleLock]:
        """Выполняет один проход сторожа и возвращает снятые блокировки."""
        await self._locks.register_instance()
        stale = await self._locks.reap()
        if stale:
            await self._on_stale(stale)

        # мёртвые инстансы после снятия блокировок: каскад спрятал бы их от обработчика
        dead = await self._locks.reap_instances()
        if dead:
            logger.warning("dead instances removed: %s", ", ".join(dead))

        await self._on_sweep()
        return stale

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._period_sec)
            try:
                await self.sweep()
            except (LockStoreError, MessageBusError, PayloadStoreError) as exc:
                logger.warning(
                    "lock reaper sweep failed, next try in %ds: %s",
                    self._period_sec,
                    exc,
                    exc_info=True,
                )
