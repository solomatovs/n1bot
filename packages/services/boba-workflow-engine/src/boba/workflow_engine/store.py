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
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema, SqlNames
from boba.workflow import RunState, RunStatus, WorkflowSpec
from boba.workflow.ports import WorkflowRepository
from boba.workflow.records import (
    DraftKey,
    DraftsColumn,
    RunsColumn,
    StoredRun,
    StoredWorkflow,
    WorkflowDraft,
    WorkflowNotFoundError,
    WorkflowsColumn,
    WorkflowStoreError,
    WorkflowTable,
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
        min_length=1,
        description="Схема postgres, в которой живут таблицы.",
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
        return SqlNames.table(self._cfg.db_schema, WorkflowTable.WORKFLOWS)

    def _runs(self) -> sql.Identifier:
        return SqlNames.table(self._cfg.db_schema, WorkflowTable.RUNS)

    def _drafts(self) -> sql.Identifier:
        return SqlNames.table(self._cfg.db_schema, WorkflowTable.DRAFTS)

    def _names(self) -> dict[str, sql.Composable]:
        """Имена из enum'ов: w_* — workflows, r_* — runs, d_* — drafts."""
        names: dict[str, sql.Composable] = {
            "workflows": self._workflows(),
            "runs": self._runs(),
            "drafts": self._drafts(),
        }
        for column in WorkflowsColumn:
            names[f"w_{column.value}"] = SqlNames.ident(column)
        for column in RunsColumn:
            names[f"r_{column.value}"] = SqlNames.ident(column)
        for column in DraftsColumn:
            names[f"d_{column.value}"] = SqlNames.ident(column)

        return names

    def _sql(self, text: LiteralString) -> sql.Composed:
        return sql.SQL(text).format(**self._names())

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
            await PostgresSchema.ensure(conn, self._cfg.db_schema)

            for query in self._ddl():
                await conn.execute(query, prepare=False)

        logger.info("workflow store ready: %s", self._cfg.db_schema)

    def _ddl(self) -> tuple[sql.Composed, ...]:
        return (
            self._sql(
                """
                create table if not exists {workflows} (
                    {w_id}         uuid primary key default gen_random_uuid(),
                    {w_user_id}    uuid not null,
                    {w_name}       text not null,
                    {w_spec}       text not null,
                    {w_tools}      text[] not null default '{{}}',
                    {w_layout}     jsonb not null default '{{}}'::jsonb,
                    {w_created_at} timestamptz not null default now(),
                    {w_updated_at} timestamptz not null default now(),
                    unique ({w_user_id}, {w_name})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {runs} (
                    {r_id}          uuid primary key,
                    {r_workflow_id} uuid references {workflows} ({w_id})
                                    on delete set null,
                    {r_user_id}     uuid not null,
                    {r_initiator}   jsonb not null,
                    {r_profile}     text not null,
                    {r_status}      text not null,
                    {r_state}       jsonb not null,
                    {r_instance}    text not null,
                    {r_started_at}  timestamptz not null default now(),
                    {r_finished_at} timestamptz
                )
                """
            ),
            self._sql(
                """
                create table if not exists {drafts} (
                    {d_user_id}    uuid not null,
                    {d_key}        text not null,
                    {d_revision}   integer not null default 1,
                    {d_spec}       text not null,
                    {d_layout}     jsonb not null default '{{}}'::jsonb,
                    {d_updated_at} timestamptz not null default now(),
                    primary key ({d_user_id}, {d_key})
                )
                """
            ),
            self._sql(
                """
                create index if not exists idx_workflow_runs_user
                    on {runs} ({r_user_id}, {r_started_at} desc)
                """
            ),
            self._sql(
                """
                create index if not exists idx_workflow_runs_status
                    on {runs} ({r_status})
                """
            ),
            self._sql("alter table {runs} drop column if exists spec"),
            self._sql(
                """
                update {runs}
                set {r_state} = jsonb_build_object(
                    'graph', jsonb_build_object(
                        'spec', {r_state} -> 'spec',
                        'stages', {r_state} -> 'stages',
                        'bindings', '{{}}'::jsonb
                    ),
                    'status', {r_state} -> 'status',
                    'tasks', {r_state} -> 'tasks'
                )
                where {r_state} ? 'spec'
                """
            ),
        )

    async def save(
        self, user_id: UUID, spec: WorkflowSpec, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Создаёт или переписывает определение владельца с этим именем."""
        query = self._sql(
            """
            insert into {workflows} (
                {w_user_id},
                {w_name},
                {w_spec},
                {w_tools},
                {w_layout}
            )
            values (
                %(user_id)s,
                %(name)s,
                %(spec)s,
                %(tools)s,
                %(layout)s
            )
            on conflict ({w_user_id}, {w_name}) do update set
                {w_spec}       = excluded.{w_spec},
                {w_tools}      = excluded.{w_tools},
                {w_layout}     = excluded.{w_layout},
                {w_updated_at} = now()
            returning
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout}, {w_created_at}, {w_updated_at}
            """
        )
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

    async def get(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        where = self._sql("w.{w_id} = %(key)s")

        return await self._one_workflow(user_id, where, workflow_id)

    async def put_draft(
        self, user_id: UUID, key: DraftKey, spec: str, layout: Mapping[str, Any]
    ) -> WorkflowDraft:
        query = self._sql(
            """
            insert into {drafts} ({d_user_id}, {d_key}, {d_spec}, {d_layout})
            values (%(user_id)s, %(key)s, %(spec)s, %(layout)s)
            on conflict ({d_user_id}, {d_key}) do update set
                {d_revision}   = {drafts}.{d_revision} + 1,
                {d_spec}       = excluded.{d_spec},
                {d_layout}     = excluded.{d_layout},
                {d_updated_at} = now()
            returning
                {d_key}, {d_user_id}, {d_revision}, {d_spec}, {d_layout}, {d_updated_at}
            """
        )
        params = {
            "user_id": user_id,
            "key": key.render(),
            "spec": spec,
            "layout": Jsonb(dict(layout)),
        }

        pool = await self._pool()
        async with self._guarded("put_draft"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"draft {key.render()!r} was not saved"
            raise WorkflowStoreError(msg)

        return WorkflowDraft.model_validate(dict(row))

    async def get_draft(self, user_id: UUID, key: DraftKey) -> WorkflowDraft:
        query = self._sql(
            """
            select
                {d_key}, {d_user_id}, {d_revision}, {d_spec}, {d_layout}, {d_updated_at}
            from
                {drafts}
            where 1=1
                and {d_user_id} = %(user_id)s
                and {d_key} = %(key)s
            """
        )

        pool = await self._pool()
        async with self._guarded("get_draft"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id, "key": key.render()})
            row = await cur.fetchone()

        if row is None:
            msg = f"draft {key.render()!r} not found"
            raise WorkflowNotFoundError(msg)

        return WorkflowDraft.model_validate(dict(row))

    async def drop_draft(self, user_id: UUID, key: DraftKey) -> bool:
        query = self._sql(
            """
            delete from {drafts}
            where 1=1
                and {d_user_id} = %(user_id)s
                and {d_key} = %(key)s
            """
        )

        pool = await self._pool()
        async with self._guarded("drop_draft"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id, "key": key.render()})
            return cur.rowcount > 0

    async def get_by_name(self, user_id: UUID, name: str) -> StoredWorkflow:
        where = self._sql("w.{w_name} = %(key)s")

        return await self._one_workflow(user_id, where, name)

    async def list_for(self, user_id: UUID) -> Sequence[StoredWorkflow]:
        query = self._sql(
            """
            select
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout}, {w_created_at}, {w_updated_at}
            from
                {workflows} w
            where
                {w_user_id} = %(user_id)s
            order by
                {w_name}
            """
        )

        pool = await self._pool()
        async with self._guarded("list"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id})
            rows = await cur.fetchall()

        found: list[StoredWorkflow] = []
        for row in rows:
            found.append(StoredWorkflow.model_validate(dict(row)))

        return found

    async def delete(self, user_id: UUID, workflow_id: UUID) -> bool:
        """Удаляет определение владельца; False — такого не было."""
        query = self._sql(
            """
            delete from {workflows}
            where
                {w_id} = %(id)s
                and {w_user_id} = %(user_id)s
            """
        )

        pool = await self._pool()
        async with self._guarded("delete"), pool.cursor() as cur:
            await cur.execute(query, {"id": workflow_id, "user_id": user_id})
            return cur.rowcount > 0

    async def start_run(  # noqa: PLR0913 — запуск описывается всеми полями сразу
        self,
        run_id: UUID,
        workflow_id: UUID | None,
        user_id: UUID,
        initiator: Mapping[str, Any],
        profile: str,
        state: RunState,
        instance: str,
    ) -> StoredRun:
        """Запись о запуске в момент старта; граф — в снимке состояния."""
        query = self._sql(
            """
            insert into {runs} (
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_status}, {r_state}, {r_instance}
            )
            values (
                %(id)s, %(workflow_id)s, %(user_id)s, %(initiator)s, %(profile)s,
                %(status)s, %(state)s, %(instance)s
            )
            returning
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            """
        )
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
        query = self._sql(
            """
            update {runs} set
                {r_status}      = %(status)s,
                {r_state}       = %(state)s,
                {r_finished_at} = case
                    when %(terminal)s then coalesce({r_finished_at}, now())
                    else {r_finished_at}
                end
            where
                {r_id} = %(id)s
            """
        )
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

    async def get_run(self, user_id: UUID, run_id: UUID) -> StoredRun:
        query = self._sql(
            """
            select
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            from
                {runs}
            where 1=1
                and {r_id} = %(id)s
                and {r_user_id} = %(user_id)s
            """
        )

        pool = await self._pool()
        async with self._guarded("get run"), pool.dict_cursor() as cur:
            await cur.execute(query, {"id": run_id, "user_id": user_id})
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: run {run_id} not found"
            raise WorkflowNotFoundError(msg)

        return StoredRun.model_validate(dict(row))

    async def run_by_id(self, run_id: UUID) -> StoredRun:
        """Запуск по id без владельца: для получателей шины после проверки подписки."""
        query = self._sql(
            """
            select
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            from
                {runs}
            where
                {r_id} = %(id)s
            """
        )

        pool = await self._pool()
        async with self._guarded("get run"), pool.dict_cursor() as cur:
            await cur.execute(query, {"id": run_id})
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: run {run_id} not found"
            raise WorkflowNotFoundError(msg)

        return StoredRun.model_validate(dict(row))

    async def list_runs(self, user_id: UUID, limit: int) -> Sequence[StoredRun]:
        query = self._sql(
            """
            select
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            from
                {runs}
            where
                {r_user_id} = %(user_id)s
            order by
                {r_started_at} desc
            limit %(limit)s
            """
        )

        pool = await self._pool()
        async with self._guarded("list runs"), pool.dict_cursor() as cur:
            await cur.execute(query, {"user_id": user_id, "limit": limit})
            rows = await cur.fetchall()

        found: list[StoredRun] = []
        for row in rows:
            found.append(StoredRun.model_validate(dict(row)))

        return found

    async def running(self) -> Sequence[StoredRun]:
        """Незавершённые запуски всех инстансов: их сверяет с блокировками сторож."""
        query = self._sql(
            """
            select
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            from
                {runs}
            where
                {r_status} = any(%(statuses)s)
            order by
                {r_started_at}
            """
        )
        params = {"statuses": [RunStatus.PENDING.value, RunStatus.RUNNING.value]}

        pool = await self._pool()
        async with self._guarded("list running"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        found: list[StoredRun] = []
        for row in rows:
            found.append(StoredRun.model_validate(dict(row)))

        return found

    async def orphans_of(self, instance: str) -> Sequence[StoredRun]:
        """Незавершённые запуски этого инстанса: после перезапуска их никто не ведёт."""
        query = self._sql(
            """
            select
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator}, {r_profile}, {r_state}, {r_instance},
                {r_started_at}, {r_finished_at}
            from
                {runs}
            where 1=1
                and {r_instance} = %(instance)s
                and {r_status} = any(%(statuses)s)
            order by
                {r_started_at}
            """
        )
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
        self, user_id: UUID, where: sql.Composable, key: object
    ) -> StoredWorkflow:
        query = sql.SQL(
            """
            select
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout}, {w_created_at}, {w_updated_at}
            from
                {workflows} w
            where 1=1
                and {w_user_id} = %(user_id)s
                and {where}
            """
        ).format(where=where, **self._names())

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
