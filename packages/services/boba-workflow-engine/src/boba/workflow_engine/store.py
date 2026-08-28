"""Таблицы workflows/workflow_runs: определения workflow и факты их запусков.

Определение — YAML-спека как сохранили плюс layout редактора; запуск —
снимок спеки, инициатор, субъект, инстанс-исполнитель и `RunState` целиком
одним jsonb. Результаты и логи задач в базе не живут: они в журнале вызовов
по `call_id` из `state`. Владелец видит только своё.

Ошибки:
WorkflowStoreError — таблицы недоступны или запись не сохранена.
WorkflowNotFoundError — определения или запуска с таким id у владельца нет.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.workflow import RunState, RunStatus, WorkflowSpec
from boba.workflow.ports import WorkflowRepository
from boba.workflow.records import (
    StoredRun,
    StoredWorkflow,
    WorkflowNotFoundError,
    WorkflowStoreError,
)

__all__ = [
    "WorkflowConfig",
    "WorkflowStore",
]

logger = logging.getLogger(__name__)


class WorkflowConfig(BaseModel):
    """Секция [workflow]: где лежат таблицы определений и запусков."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Создавать таблицы workflows/workflow_runs при старте.",
    )
    connection: PostgresConfig | None = Field(
        default=None,
        description='Postgres-профиль ссылкой: connection = "${postgres}".',
    )
    db_schema: str = Field(
        default="chainlit",
        description="Схема postgres, в которой живут таблицы.",
    )
    table: str = Field(
        default="workflows",
        description="Имя таблицы определений.",
    )
    runs_table: str = Field(
        default="workflow_runs",
        description="Имя таблицы запусков.",
    )

    def require_conn(self) -> PostgresConfig:
        if self.connection is None:
            msg = 'workflow.connection is not set: connection = "${postgres}"'
            raise ValueError(msg)

        return self.connection


class WorkflowStore(WorkflowRepository):
    """CRUD над workflows/workflow_runs; всё чтение и запись — под владельцем."""

    def __init__(
        self,
        cfg: WorkflowConfig,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        self._cfg = cfg
        self._pool_ref = pool

    async def _pool(self) -> AsyncPostgresPool:
        """Пул берётся при первом обращении: __init__ не может await."""
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._cfg.require_conn())

        return self._pool_ref

    def _workflows(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.table)

    def _runs(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.runs_table)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None, None]:
        """Граница слоя: отказ базы или пула уходит наружу как WorkflowStoreError."""
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            msg = f"workflow: {action} failed"
            raise WorkflowStoreError(msg) from exc

    async def setup(self) -> None:
        pool = await self._pool()

        async with self._guarded("setup"), pool.connection() as conn:
            try:
                await conn.execute(
                    sql.SQL("create schema if not exists {schema}").format(
                        schema=sql.Identifier(self._cfg.db_schema),
                    ),
                    prepare=False,
                )
            except InsufficientPrivilege:
                logger.info(
                    "no permission for create schema %r, "
                    "assuming an administrator created it",
                    self._cfg.db_schema,
                )

            for query in self._ddl():
                await conn.execute(query, prepare=False)

        logger.info(
            "workflow store ready: %s.%s, %s.%s",
            self._cfg.db_schema,
            self._cfg.table,
            self._cfg.db_schema,
            self._cfg.runs_table,
        )

    def _ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {workflows} (
                    id         integer generated always as identity primary key,
                    user_id    integer not null,
                    name       text not null,
                    spec       text not null,
                    tools      text[] not null default '{{}}',
                    layout     jsonb not null default '{{}}'::jsonb,
                    created_at timestamptz not null default now(),
                    updated_at timestamptz not null default now(),
                    unique (user_id, name)
                )
                """
            ).format(workflows=self._workflows()),
            sql.SQL(
                """
                create table if not exists {runs} (
                    id          uuid primary key,
                    workflow_id integer references {workflows} (id) on delete set null,
                    user_id     integer not null,
                    initiator   jsonb not null,
                    profile     text not null,
                    status      text not null,
                    state       jsonb not null,
                    instance    text not null,
                    started_at  timestamptz not null default now(),
                    finished_at timestamptz
                )
                """
            ).format(runs=self._runs(), workflows=self._workflows()),
            sql.SQL(
                """
                create index if not exists idx_workflow_runs_user
                    on {runs} (user_id, started_at desc)
                """
            ).format(runs=self._runs()),
            sql.SQL(
                """
                create index if not exists idx_workflow_runs_status
                    on {runs} (status)
                """
            ).format(runs=self._runs()),
            sql.SQL("alter table {runs} drop column if exists spec").format(
                runs=self._runs()
            ),
            sql.SQL(
                """
                update {runs}
                set state = jsonb_build_object(
                    'graph', jsonb_build_object(
                        'spec', state -> 'spec',
                        'stages', state -> 'stages',
                        'bindings', '{{}}'::jsonb
                    ),
                    'status', state -> 'status',
                    'tasks', state -> 'tasks'
                )
                where state ? 'spec'
                """
            ).format(runs=self._runs()),
        )

    async def save(
        self, user_id: int, spec: WorkflowSpec, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Создаёт или переписывает определение владельца с этим именем."""
        query = sql.SQL(
            """
            insert into {workflows} (
                user_id,
                name,
                spec,
                tools,
                layout
            )
            values (
                %(user_id)s,
                %(name)s,
                %(spec)s,
                %(tools)s,
                %(layout)s
            )
            on conflict (user_id, name) do update set
                spec       = excluded.spec,
                tools      = excluded.tools,
                layout     = excluded.layout,
                updated_at = now()
            returning
                id, user_id, name, spec, tools, layout, created_at, updated_at
            """
        ).format(workflows=self._workflows())
        params = {
            "user_id": user_id,
            "name": spec.name,
            "spec": spec.render_yaml(),
            "tools": self._tools_of(spec),
            "layout": Jsonb(dict(layout)),
        }

        pool = await self._pool()
        async with self._guarded("save"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: {spec.name!r} was not saved"
            raise WorkflowStoreError(msg)

        return StoredWorkflow.model_validate(dict(row))

    async def get(self, user_id: int, workflow_id: int) -> StoredWorkflow:
        return await self._one_workflow(user_id, sql.SQL("w.id = %(key)s"), workflow_id)

    async def get_by_name(self, user_id: int, name: str) -> StoredWorkflow:
        return await self._one_workflow(user_id, sql.SQL("w.name = %(key)s"), name)

    async def list_for(self, user_id: int) -> Sequence[StoredWorkflow]:
        query = sql.SQL(
            """
            select
                id, user_id, name, spec, tools, layout, created_at, updated_at
            from
                {workflows} w
            where
                user_id = %(user_id)s
            order by
                name
            """
        ).format(workflows=self._workflows())

        pool = await self._pool()
        async with self._guarded("list"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id})
            rows = await cur.fetchall()

        found: list[StoredWorkflow] = []
        for row in rows:
            found.append(StoredWorkflow.model_validate(dict(row)))

        return found

    async def delete(self, user_id: int, workflow_id: int) -> bool:
        """Удаляет определение владельца; False — такого не было."""
        query = sql.SQL(
            """
            delete from {workflows}
            where
                id = %(id)s
                and user_id = %(user_id)s
            """
        ).format(workflows=self._workflows())

        pool = await self._pool()
        async with self._guarded("delete"), pool.cursor() as cur:
            await cur.execute(query, {"id": workflow_id, "user_id": user_id})
            return cur.rowcount > 0

    async def start_run(  # noqa: PLR0913 — запуск описывается всеми полями сразу
        self,
        run_id: UUID,
        workflow_id: int | None,
        user_id: int,
        initiator: Mapping[str, Any],
        profile: str,
        state: RunState,
        instance: str,
    ) -> StoredRun:
        """Запись о запуске в момент старта; граф — в снимке состояния."""
        query = sql.SQL(
            """
            insert into {runs} (
                id, workflow_id, user_id, initiator, profile, status, state, instance
            )
            values (
                %(id)s, %(workflow_id)s, %(user_id)s, %(initiator)s, %(profile)s,
                %(status)s, %(state)s, %(instance)s
            )
            returning
                id, workflow_id, user_id, initiator, profile, state, instance,
                started_at, finished_at
            """
        ).format(runs=self._runs())
        params = {
            "id": run_id,
            "workflow_id": workflow_id,
            "user_id": user_id,
            "initiator": Jsonb(dict(initiator)),
            "profile": profile,
            "status": state.status.value,
            "state": Jsonb(state.model_dump(mode="json")),
            "instance": instance,
        }

        pool = await self._pool()
        async with self._guarded("start run"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: run {run_id} was not saved"
            raise WorkflowStoreError(msg)

        return StoredRun.model_validate(dict(row))

    async def update_run(self, run_id: UUID, state: RunState) -> None:
        """Свежий снимок состояния; завершённый запуск получает finished_at."""
        query = sql.SQL(
            """
            update {runs} set
                status      = %(status)s,
                state       = %(state)s,
                finished_at = case
                    when %(terminal)s then coalesce(finished_at, now())
                    else finished_at
                end
            where
                id = %(id)s
            """
        ).format(runs=self._runs())
        params = {
            "id": run_id,
            "status": state.status.value,
            "state": Jsonb(state.model_dump(mode="json")),
            "terminal": state.status.terminal,
        }

        pool = await self._pool()
        async with self._guarded("update run"), pool.cursor() as cur:
            await cur.execute(query, params)
            if cur.rowcount == 0:
                msg = f"workflow: run {run_id} not found"
                raise WorkflowNotFoundError(msg)

    async def get_run(self, user_id: int, run_id: UUID) -> StoredRun:
        query = sql.SQL(
            """
            select
                id, workflow_id, user_id, initiator, profile, state, instance,
                started_at, finished_at
            from
                {runs}
            where 1=1
                and id = %(id)s
                and user_id = %(user_id)s
            """
        ).format(runs=self._runs())

        pool = await self._pool()
        async with self._guarded("get run"), pool.dict_cursor() as cur:
            await cur.execute(query, {"id": run_id, "user_id": user_id})
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: run {run_id} not found"
            raise WorkflowNotFoundError(msg)

        return StoredRun.model_validate(dict(row))

    async def list_runs(self, user_id: int, limit: int) -> Sequence[StoredRun]:
        query = sql.SQL(
            """
            select
                id, workflow_id, user_id, initiator, profile, state, instance,
                started_at, finished_at
            from
                {runs}
            where
                user_id = %(user_id)s
            order by
                started_at desc
            limit %(limit)s
            """
        ).format(runs=self._runs())

        pool = await self._pool()
        async with self._guarded("list runs"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id, "limit": limit})
            rows = await cur.fetchall()

        found: list[StoredRun] = []
        for row in rows:
            found.append(StoredRun.model_validate(dict(row)))

        return found

    async def orphans_of(self, instance: str) -> Sequence[StoredRun]:
        """Незавершённые запуски этого инстанса: после перезапуска их никто не ведёт."""
        query = sql.SQL(
            """
            select
                id, workflow_id, user_id, initiator, profile, state, instance,
                started_at, finished_at
            from
                {runs}
            where 1=1
                and instance = %(instance)s
                and status = any(%(statuses)s)
            order by
                started_at
            """
        ).format(runs=self._runs())
        params = {
            "instance": instance,
            "statuses": [RunStatus.PENDING.value, RunStatus.RUNNING.value],
        }

        pool = await self._pool()
        async with self._guarded("list orphans"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        found: list[StoredRun] = []
        for row in rows:
            found.append(StoredRun.model_validate(dict(row)))

        return found

    async def _one_workflow(
        self, user_id: int, where: sql.Composable, key: object
    ) -> StoredWorkflow:
        query = sql.SQL(
            """
            select
                id, user_id, name, spec, tools, layout, created_at, updated_at
            from
                {workflows} w
            where 1=1
                and user_id = %(user_id)s
                and {where}
            """
        ).format(workflows=self._workflows(), where=where)

        pool = await self._pool()
        async with self._guarded("get"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id, "key": key})
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: {key!r} not found"
            raise WorkflowNotFoundError(msg)

        return StoredWorkflow.model_validate(dict(row))

    @staticmethod
    def _tools_of(spec: WorkflowSpec) -> list[str]:
        names: list[str] = []
        for task in spec.tasks.values():
            if task.tool in names:
                continue

            names.append(task.tool)

        return sorted(names)
