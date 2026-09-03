"""Таблицы workflows/workflow_runs: определения workflow и факты их запусков.

Определение — YAML-спека как сохранили плюс layout редактора; запуск —
снимок спеки, инициатор, субъект, инстанс-исполнитель и `RunState` целиком
одним jsonb. Результаты и логи задач в базе не живут: они в журнале вызовов
по `call_id` из `state`. Владелец видит только своё.

Ошибки:
WorkflowStoreError — таблицы недоступны или запись не сохранена.
WorkflowNotFoundError — определения или запуска с таким id у владельца нет.
WorkflowNameTakenError — имя занято другой строкой того же пользователя.
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

from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames
from boba.db.postgres.profile import PostgresConfig
from boba.workflow import RunState, RunStatus, WorkflowSpec
from boba.workflow.ports import WorkflowRepository
from boba.workflow.records import (
    RunsColumn,
    StoredRun,
    StoredWorkflow,
    WorkflowNameTakenError,
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


class WorkflowStore(PostgresTable, WorkflowRepository):
    """CRUD над workflows/workflow_runs; всё чтение и запись — под владельцем."""

    def __init__(
        self,
        cfg: WorkflowConfig,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg

    def _workflows(self) -> sql.Identifier:
        return self._table(WorkflowTable.WORKFLOWS)

    def _runs(self) -> sql.Identifier:
        return self._table(WorkflowTable.RUNS)

    def _names(self) -> dict[str, sql.Composable]:
        """Имена из enum'ов: w_* — workflows, r_* — runs."""
        names: dict[str, sql.Composable] = {
            "workflows": self._workflows(),
            "runs": self._runs(),
        }
        for column in WorkflowsColumn:
            names[f"w_{column.value}"] = SqlNames.ident(column)
        for column in RunsColumn:
            names[f"r_{column.value}"] = SqlNames.ident(column)
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
        """Схема, три таблицы и миграции; повтор безвреден."""
        ddl = (
            *self._workflows_ddl(),
            *self._runs_ddl(),
            *self._migrations(),
        )
        async with self._guarded("setup"):
            await self._apply_ddl(ddl)

        logger.info("workflow store ready: %s", self._cfg.db_schema)

    def _workflows_ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {workflows} (
                    {id}         uuid primary key default gen_random_uuid(),
                    {user_id}    uuid not null,
                    {name}       text not null,
                    {spec}       text not null,
                    {tools}      text[] not null default '{{}}',
                    {layout}     jsonb not null default '{{}}'::jsonb,
                    {created_at} timestamptz not null default now(),
                    {updated_at} timestamptz not null default now(),
                    {draft_spec}     text,
                    {draft_layout}   jsonb,
                    {draft_revision} int not null default 0,
                    unique ({user_id}, {name})
                )
                """
            ).format(workflows=self._workflows(), **self._columns(WorkflowsColumn)),
            sql.SQL(
                """
                alter table {workflows}
                    add column if not exists {draft_spec} text,
                    add column if not exists {draft_layout} jsonb,
                    add column if not exists {draft_revision} int not null default 0
                """
            ).format(workflows=self._workflows(), **self._columns(WorkflowsColumn)),
        )

    def _runs_ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {runs} (
                    {id}          uuid primary key,
                    {workflow_id} uuid references {workflows} ({workflow_pk})
                                  on delete set null,
                    {user_id}     uuid not null,
                    {initiator}   jsonb not null,
                    {profile}     text not null,
                    {status}      text not null,
                    {state}       jsonb not null,
                    {instance}    text not null,
                    {started_at}  timestamptz not null default now(),
                    {finished_at} timestamptz
                )
                """
            ).format(
                runs=self._runs(),
                workflows=self._workflows(),
                workflow_pk=SqlNames.ident(WorkflowsColumn.ID),
                **self._columns(RunsColumn),
            ),
            sql.SQL(
                """
                create index if not exists idx_workflow_runs_user
                    on {runs} ({user_id}, {started_at} desc)
                """
            ).format(runs=self._runs(), **self._columns(RunsColumn)),
            sql.SQL(
                """
                create index if not exists idx_workflow_runs_status
                    on {runs} ({status})
                """
            ).format(runs=self._runs(), **self._columns(RunsColumn)),
        )

    def _migrations(self) -> tuple[sql.Composed, ...]:
        """Перевод старых строк runs: spec ушёл из колонки в state.graph."""
        return (
            sql.SQL("alter table {runs} drop column if exists spec").format(
                runs=self._runs()
            ),
            sql.SQL(
                """
                update {runs}
                set {state} = jsonb_build_object(
                    'graph', jsonb_build_object(
                        'spec', {state} -> 'spec',
                        'stages', {state} -> 'stages',
                        'bindings', '{{}}'::jsonb
                    ),
                    'status', {state} -> 'status',
                    'tasks', {state} -> 'tasks'
                )
                where {state} ? 'spec'
                """
            ).format(runs=self._runs(), **self._columns(RunsColumn)),
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
                {w_spec}           = excluded.{w_spec},
                {w_tools}          = excluded.{w_tools},
                {w_layout}         = excluded.{w_layout},
                {w_draft_spec}     = null,
                {w_draft_layout}   = null,
                {w_draft_revision} = {workflows}.{w_draft_revision} + 1,
                {w_updated_at}     = now()
            returning
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
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

    async def save_into(
        self,
        user_id: UUID,
        workflow_id: UUID,
        spec: WorkflowSpec,
        layout: Mapping[str, Any],
    ) -> StoredWorkflow:
        """Переписывает строку по id: имя, спека, раскладка; черновик снимается."""
        query = self._sql(
            """
            update {workflows} set
                {w_name}           = %(name)s,
                {w_spec}           = %(spec)s,
                {w_tools}          = %(tools)s,
                {w_layout}         = %(layout)s,
                {w_draft_spec}     = null,
                {w_draft_layout}   = null,
                {w_draft_revision} = {w_draft_revision} + 1,
                {w_updated_at}     = now()
            where 1=1
                and {w_id} = %(id)s
                and {w_user_id} = %(user_id)s
            returning
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
            """
        )
        params = {
            "id": workflow_id,
            "user_id": user_id,
            "name": spec.name,
            "spec": spec.render_yaml(),
            "tools": self._tools_of(spec),
            "layout": Jsonb(dict(layout)),
        }

        pool = await self._pool()
        async with self._guarded("save_into"), pool.dict_cursor() as cur:
            try:
                await cur.execute(query, params)
            except psycopg.errors.UniqueViolation as exc:
                msg = f"workflow name already taken: {spec.name!r}"
                raise WorkflowNameTakenError(msg) from exc

            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: {workflow_id!r} not found"
            raise WorkflowNotFoundError(msg)

        return StoredWorkflow.model_validate(dict(row))

    async def get(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        where = self._sql("w.{w_id} = %(key)s")

        return await self._one_workflow(user_id, where, workflow_id)

    async def put_draft(
        self, user_id: UUID, workflow_id: UUID, spec: str, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Пишет черновик в строку workflow; draft_revision растёт на единицу."""
        query = self._sql(
            """
            update {workflows} set
                {w_draft_spec}     = %(spec)s,
                {w_draft_layout}   = %(layout)s,
                {w_draft_revision} = {w_draft_revision} + 1
            where 1=1
                and {w_id} = %(id)s
                and {w_user_id} = %(user_id)s
            returning
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
            """
        )
        params = {
            "id": workflow_id,
            "user_id": user_id,
            "spec": spec,
            "layout": Jsonb(dict(layout)),
        }

        pool = await self._pool()
        async with self._guarded("put_draft"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: {workflow_id!r} not found"
            raise WorkflowNotFoundError(msg)

        return StoredWorkflow.model_validate(dict(row))

    async def clear_draft(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        """Сбрасывает черновик: строка возвращается к сохранённому состоянию."""
        query = self._sql(
            """
            update {workflows} set
                {w_draft_spec}     = null,
                {w_draft_layout}   = null,
                {w_draft_revision} = {w_draft_revision} + 1
            where 1=1
                and {w_id} = %(id)s
                and {w_user_id} = %(user_id)s
            returning
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
            """
        )

        pool = await self._pool()
        async with self._guarded("clear_draft"), pool.dict_cursor() as cur:
            await cur.execute(query, {"id": workflow_id, "user_id": user_id})
            row = await cur.fetchone()

        if row is None:
            msg = f"workflow: {workflow_id!r} not found"
            raise WorkflowNotFoundError(msg)

        return StoredWorkflow.model_validate(dict(row))

    async def get_by_name(self, user_id: UUID, name: str) -> StoredWorkflow:
        where = self._sql("w.{w_name} = %(key)s")

        return await self._one_workflow(user_id, where, name)

    async def list_for(self, user_id: UUID) -> Sequence[StoredWorkflow]:
        query = self._sql(
            """
            select
                {w_id}, {w_user_id}, {w_name}, {w_spec},
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_status}, {r_state}, {r_instance}
            )
            values (
                %(id)s, %(workflow_id)s, %(user_id)s, %(initiator)s, %(profile)s,
                %(status)s, %(state)s, %(instance)s
            )
            returning
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {r_id}, {r_workflow_id}, {r_user_id}, {r_initiator},
                {r_profile}, {r_state}, {r_instance},
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
                {w_tools}, {w_layout},
                {w_draft_spec}, {w_draft_layout}, {w_draft_revision},
                {w_created_at}, {w_updated_at}
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
