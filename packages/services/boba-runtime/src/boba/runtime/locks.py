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
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.identity.context import Scope, ScopeKind
from boba.identity.locks import (
    LiveLock,
    LiveLocks,
    LockBusy,
    LockBusyError,
    LockMode,
    LockPurpose,
    LockToken,
    StaleLock,
)
from boba.messaging import MessageBusError
from boba.runtime.config import AppName, ClusterConfig
from boba.runtime.payloads import PayloadStoreError
from boba.runtime.tables import ChatTable, LiveInstancesColumn, LiveLocksColumn

__all__ = ["LockReaper", "LockStoreError", "PgLiveLocks"]

logger = logging.getLogger(__name__)


class LockStoreError(Exception):
    """База блокировок недоступна или запрос не выполнен."""


class PgLiveLocks(LiveLocks):
    """Захватывает, подтверждает и снимает блокировки в live_locks; протухание
    считается по часам Postgres, чтобы расхождение часов узлов не влияло.
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

    @property
    def instance(self) -> str:
        return self._instance

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._cfg)

        return self._pool_ref

    def _locks(self) -> sql.Identifier:
        return ChatTable.LIVE_LOCKS.under(self._schema)

    def _instances(self) -> sql.Identifier:
        return ChatTable.LIVE_INSTANCES.under(self._schema)

    @staticmethod
    def _scope_id(scope: Scope) -> UUID:
        try:
            return UUID(scope.id)
        except ValueError as exc:
            msg = f"scope id is not a uuid: {scope.id!r}"
            raise LockStoreError(msg) from exc

    async def acquire(
        self, scope: Scope, mode: LockMode, purpose: LockPurpose, user_id: UUID
    ) -> LiveLock:
        scope_id = self._scope_id(scope)
        token = LockToken.local()
        pool = await self._pool()
        params = {"scope_kind": scope.kind.value, "scope_id": scope_id}

        try:
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                    {"key": scope.render()},
                    prepare=False,
                )
                await conn.execute(
                    sql.SQL(
                        """
                        delete from {locks}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                            and {heartbeat} + make_interval(secs => {ttl}) < now()
                        """
                    ).format(
                        locks=self._locks(),
                        scope_kind=LiveLocksColumn.SCOPE_KIND.ident(),
                        scope_id=LiveLocksColumn.SCOPE_ID.ident(),
                        heartbeat=LiveLocksColumn.HEARTBEAT_AT.ident(),
                        ttl=LiveLocksColumn.TTL_SEC.ident(),
                    ),
                    params,
                    prepare=False,
                )
                holders = await self._holders(conn, scope)
                for busy in holders:
                    if mode is LockMode.EXCLUSIVE:
                        raise LockBusyError(scope, busy)

                    if busy.mode is LockMode.EXCLUSIVE:
                        raise LockBusyError(scope, busy)

                await conn.execute(
                    sql.SQL(
                        """
                        insert into {locks}
                            ({scope_kind}, {scope_id}, {mode}, {holder}, {token},
                             {purpose}, {user_id}, {ttl})
                        values (%(scope_kind)s, %(scope_id)s, %(mode)s, %(holder)s,
                                %(token)s, %(purpose)s, %(user_id)s, %(ttl)s)
                        """
                    ).format(
                        locks=self._locks(),
                        scope_kind=LiveLocksColumn.SCOPE_KIND.ident(),
                        scope_id=LiveLocksColumn.SCOPE_ID.ident(),
                        mode=LiveLocksColumn.MODE.ident(),
                        holder=LiveLocksColumn.HOLDER.ident(),
                        token=LiveLocksColumn.TOKEN.ident(),
                        purpose=LiveLocksColumn.PURPOSE.ident(),
                        user_id=LiveLocksColumn.USER_ID.ident(),
                        ttl=LiveLocksColumn.TTL_SEC.ident(),
                    ),
                    {
                        **params,
                        "mode": mode.value,
                        "holder": self._instance,
                        "token": token.value,
                        "purpose": purpose.value,
                        "user_id": user_id,
                        "ttl": self._cluster.lock_ttl_sec,
                    },
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"live locks: acquire of {scope.render()} failed"
            raise LockStoreError(msg) from exc

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

    async def _holders(
        self, conn: psycopg.AsyncConnection[Any], scope: Scope
    ) -> Sequence[LockBusy]:
        cur = await conn.execute(
            sql.SQL(
                """
                select {holder}, {mode}, {purpose},
                       extract(epoch from now() - {heartbeat})::int as silent
                  from {locks}
                 where {scope_kind} = %(scope_kind)s
                   and {scope_id} = %(scope_id)s
                   and {heartbeat} + make_interval(secs => {ttl}) >= now()
                """
            ).format(
                locks=self._locks(),
                holder=LiveLocksColumn.HOLDER.ident(),
                mode=LiveLocksColumn.MODE.ident(),
                purpose=LiveLocksColumn.PURPOSE.ident(),
                heartbeat=LiveLocksColumn.HEARTBEAT_AT.ident(),
                scope_kind=LiveLocksColumn.SCOPE_KIND.ident(),
                scope_id=LiveLocksColumn.SCOPE_ID.ident(),
                ttl=LiveLocksColumn.TTL_SEC.ident(),
            ),
            {"scope_kind": scope.kind.value, "scope_id": self._scope_id(scope)},
            prepare=False,
        )
        rows = await cur.fetchall()

        found: list[LockBusy] = []
        for row in rows:
            found.append(
                LockBusy(
                    holder=str(row[0]),
                    mode=LockMode(str(row[1])),
                    purpose=LockPurpose(str(row[2])),
                    silent_sec=max(int(row[3]), 0),
                )
            )

        return found

    async def holders_of(self, scope: Scope) -> Sequence[LockBusy]:
        pool = await self._pool()
        try:
            async with pool.connection() as conn:
                return await self._holders(conn, scope)
        except (psycopg.Error, PostgresError) as exc:
            msg = f"live locks: holders of {scope.render()} failed"
            raise LockStoreError(msg) from exc

    async def heartbeat(self, token: LockToken) -> bool:
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        update {locks} set {heartbeat} = now()
                         where {token} = %(token)s
                           and {heartbeat} + make_interval(secs => {ttl}) >= now()
                        """
                    ).format(
                        locks=self._locks(),
                        heartbeat=LiveLocksColumn.HEARTBEAT_AT.ident(),
                        token=LiveLocksColumn.TOKEN.ident(),
                        ttl=LiveLocksColumn.TTL_SEC.ident(),
                    ),
                    {"token": token.value},
                    prepare=False,
                )
                return cur.rowcount == 1
        except (psycopg.Error, PostgresError) as exc:
            msg = "live locks: heartbeat failed"
            raise LockStoreError(msg) from exc

    async def register_instance(self) -> None:
        """Записывает инстанс в live_instances или подтверждает его жизнь: строка
        заводится заново, если Postgres перезапустился и unlogged-таблица опустела.
        """
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        insert into {instances} ({id}, {app}, {host})
                        values (%(id)s, %(app)s, %(host)s)
                        on conflict ({id}) do update
                        set {app} = excluded.{app}, {host} = excluded.{host},
                            {heartbeat} = now()
                        """
                    ).format(
                        instances=self._instances(),
                        id=LiveInstancesColumn.INSTANCE_ID.ident(),
                        app=LiveInstancesColumn.APP.ident(),
                        host=LiveInstancesColumn.HOST.ident(),
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
            msg = "live locks: instance registration failed"
            raise LockStoreError(msg) from exc

    async def release(self, token: LockToken) -> None:
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL("delete from {locks} where {token} = %(token)s").format(
                        locks=self._locks(), token=LiveLocksColumn.TOKEN.ident()
                    ),
                    {"token": token.value},
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = "live locks: release failed"
            raise LockStoreError(msg) from exc

    async def release_all(self, holder: str) -> int:
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL("delete from {locks} where {holder} = %(holder)s").format(
                        locks=self._locks(), holder=LiveLocksColumn.HOLDER.ident()
                    ),
                    {"holder": holder},
                    prepare=False,
                )
                return cur.rowcount
        except (psycopg.Error, PostgresError) as exc:
            msg = "live locks: release_all failed"
            raise LockStoreError(msg) from exc

    async def reap(self) -> Sequence[StaleLock]:
        pool = await self._pool()
        try:
            async with pool.dict_cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        delete from {locks}
                         where {heartbeat} + make_interval(secs => {ttl}) < now()
                        returning {scope_kind}, {scope_id}, {holder}, {purpose}
                        """
                    ).format(
                        locks=self._locks(),
                        heartbeat=LiveLocksColumn.HEARTBEAT_AT.ident(),
                        ttl=LiveLocksColumn.TTL_SEC.ident(),
                        scope_kind=LiveLocksColumn.SCOPE_KIND.ident(),
                        scope_id=LiveLocksColumn.SCOPE_ID.ident(),
                        holder=LiveLocksColumn.HOLDER.ident(),
                        purpose=LiveLocksColumn.PURPOSE.ident(),
                    ),
                    prepare=False,
                )
                rows = await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = "live locks: reap failed"
            raise LockStoreError(msg) from exc

        return [self._stale(row) for row in rows]

    @staticmethod
    def _stale(row: DictRow) -> StaleLock:
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
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        delete from {instances}
                         where {heartbeat} + make_interval(secs => %(ttl)s) < now()
                        returning {id}
                        """
                    ).format(
                        instances=self._instances(),
                        heartbeat=LiveInstancesColumn.HEARTBEAT_AT.ident(),
                        id=LiveInstancesColumn.INSTANCE_ID.ident(),
                    ),
                    {"ttl": self._cluster.lock_ttl_sec},
                    prepare=False,
                )
                rows = await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = "live locks: reap of instances failed"
            raise LockStoreError(msg) from exc

        return [str(row[0]) for row in rows]


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
            except (LockStoreError, MessageBusError, PayloadStoreError):
                logger.warning("lock reaper sweep failed", exc_info=True)
