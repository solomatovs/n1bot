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
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PgQuery, PgQueryBuilder, PostgresPool, PostgresTable
from boba.db.postgres.connection import PostgresConfig
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
            msg = (
                "[workflow].connection is not set: expected a postgres "
                'reference such as connection = "${postgres}"'
            )
            raise ValueError(msg)

        return self.connection


class WorkflowStore(PostgresTable, WorkflowRepository):
    """CRUD над workflows/workflow_runs; всё чтение и запись — под владельцем.

    Колонки строки определения и запуска идут в запросы именами
    {workflow_columns} и {run_columns}, чтобы returning и select отдавали
    ровно те поля, из которых собираются StoredWorkflow и StoredRun.
    """

    LABEL: ClassVar[str] = "workflow"

    def __init__(
        self,
        cfg: WorkflowConfig,
        pool: PostgresPool | None = None,
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg

    def _query(self) -> PgQueryBuilder:
        return PgQueryBuilder(
            schema=self._schema.ident,
            workflow_columns=self._column_list(WorkflowsColumn),
            run_columns=self._column_list(RunsColumn.stored()),
        )

    def _failure(self, action: str, exc: Exception) -> Exception:
        return WorkflowStoreError(self._detail(action, exc))

    async def setup(self) -> None:
        """Схема, три таблицы и миграции; повтор безвреден."""
        ddl = (
            *self._workflows_ddl(),
            *self._runs_ddl(),
            *self._migrations(),
        )
        await self._apply_ddl(ddl)

        logger.info("workflow store ready: %s", self.schema)

    def _workflows_ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.workflows (
                    id             uuid primary key default gen_random_uuid(),
                    user_id        uuid not null,
                    name           text not null,
                    spec           text not null,
                    tools          text[] not null default '{{}}',
                    layout         jsonb not null default '{{}}'::jsonb,
                    created_at     timestamptz not null default now(),
                    updated_at     timestamptz not null default now(),
                    draft_spec     text,
                    draft_layout   jsonb,
                    draft_revision int not null default 0,
                    unique (user_id, name)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                alter table {schema}.workflows
                    add column if not exists draft_spec text,
                    add column if not exists draft_layout jsonb,
                    add column if not exists draft_revision int not null default 0
                """
            )
            .build(),
        )

    def _runs_ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.workflow_runs (
                    id          uuid primary key,
                    workflow_id uuid references {schema}.workflows (id)
                                on delete set null,
                    user_id     uuid not null,
                    initiator   jsonb not null,
                    profile     text not null,
                    status      text not null,
                    state       jsonb not null,
                    instance    text not null,
                    started_at  timestamptz not null default now(),
                    finished_at timestamptz
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_workflow_runs_user
                    on {schema}.workflow_runs (user_id, started_at desc)
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_workflow_runs_status
                    on {schema}.workflow_runs (status)
                """
            )
            .build(),
        )

    def _migrations(self) -> tuple[PgQuery, ...]:
        """Перевод старых строк runs: spec ушёл из колонки в state.graph."""
        return (
            self._query()
            .add("alter table {schema}.workflow_runs drop column if exists spec")
            .build(),
            self._query()
            .add(
                """
                update {schema}.workflow_runs
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
            )
            .build(),
        )

    async def save(
        self, user_id: UUID, spec: WorkflowSpec, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Создаёт или переписывает определение владельца с этим именем."""
        query = (
            self._query()
            .add(
                """
                insert into {schema}.workflows (
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
                    spec           = excluded.spec,
                    tools          = excluded.tools,
                    layout         = excluded.layout,
                    draft_spec     = null,
                    draft_layout   = null,
                    draft_revision = {schema}.workflows.draft_revision + 1,
                    updated_at     = now()
                returning
                    {workflow_columns}
                """,
                user_id=user_id,
                name=spec.name,
                spec=spec.render_yaml(),
                tools=self._tools_of(spec),
                layout=Jsonb(dict(layout)),
            )
            .build()
        )
        row = self._returning(
            await self._row(query, "save"),
            f"upsert of {spec.name!r} for user {user_id} into workflows",
        )

        return self._parse(StoredWorkflow, row)

    async def save_into(
        self,
        user_id: UUID,
        workflow_id: UUID,
        spec: WorkflowSpec,
        layout: Mapping[str, Any],
    ) -> StoredWorkflow:
        """Переписывает строку по id: имя, спека, раскладка; черновик снимается.

        Ошибки:
        WorkflowNameTakenError — имя занято другой строкой того же пользователя.
        WorkflowNotFoundError — строки с таким id у владельца нет.
        """
        query = (
            self._query()
            .add(
                """
                update {schema}.workflows set
                    name           = %(name)s,
                    spec           = %(spec)s,
                    tools          = %(tools)s,
                    layout         = %(layout)s,
                    draft_spec     = null,
                    draft_layout   = null,
                    draft_revision = draft_revision + 1,
                    updated_at     = now()
                where 1=1
                    and id = %(id)s
                    and user_id = %(user_id)s
                returning
                    {workflow_columns}
                """,
                id=workflow_id,
                user_id=user_id,
                name=spec.name,
                spec=spec.render_yaml(),
                tools=self._tools_of(spec),
                layout=Jsonb(dict(layout)),
            )
            .build()
        )

        async with self._transaction("save_into") as cur:
            try:
                await cur.execute(query.text, query.params)
            except UniqueViolation as exc:
                msg = (
                    f"workflow name {spec.name!r} is already taken by another "
                    f"workflow of user {user_id}, cannot rename #{workflow_id}: "
                    f"{exc}"
                )
                raise WorkflowNameTakenError(msg) from exc

            row = await cur.fetchone()

        if row is None:
            msg = (
                f"workflow #{workflow_id} of user {user_id} not found in "
                f"{self.schema}.workflows: nothing to save into"
            )
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredWorkflow, row)

    async def get(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        query = (
            self._query()
            .add(
                """
                select
                    {workflow_columns}
                from
                    {schema}.workflows
                where 1=1
                    and user_id = %(user_id)s
                    and id = %(id)s
                """,
                user_id=user_id,
                id=workflow_id,
            )
            .build()
        )

        return await self._one_workflow(query, user_id, workflow_id)

    async def get_by_name(self, user_id: UUID, name: str) -> StoredWorkflow:
        query = (
            self._query()
            .add(
                """
                select
                    {workflow_columns}
                from
                    {schema}.workflows
                where 1=1
                    and user_id = %(user_id)s
                    and name = %(name)s
                """,
                user_id=user_id,
                name=name,
            )
            .build()
        )

        return await self._one_workflow(query, user_id, name)

    async def _one_workflow(
        self, query: PgQuery, user_id: UUID, key: object
    ) -> StoredWorkflow:
        """Ошибки:
        WorkflowNotFoundError — строки по ключу у владельца нет.
        """
        row = await self._row(query, "get")
        if row is None:
            msg = (
                f"workflow {key!r} of user {user_id} not found in "
                f"{self.schema}.workflows"
            )
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredWorkflow, row)

    async def put_draft(
        self, user_id: UUID, workflow_id: UUID, spec: str, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Пишет черновик в строку workflow; draft_revision растёт на единицу.

        Ошибки:
        WorkflowNotFoundError — строки с таким id у владельца нет.
        """
        query = (
            self._query()
            .add(
                """
                update {schema}.workflows set
                    draft_spec     = %(spec)s,
                    draft_layout   = %(layout)s,
                    draft_revision = draft_revision + 1
                where 1=1
                    and id = %(id)s
                    and user_id = %(user_id)s
                returning
                    {workflow_columns}
                """,
                id=workflow_id,
                user_id=user_id,
                spec=spec,
                layout=Jsonb(dict(layout)),
            )
            .build()
        )
        row = await self._row(query, "put_draft")
        if row is None:
            msg = (
                f"workflow #{workflow_id} of user {user_id} not found in "
                f"{self.schema}.workflows: draft not written"
            )
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredWorkflow, row)

    async def clear_draft(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        """Сбрасывает черновик: строка возвращается к сохранённому состоянию.

        Ошибки:
        WorkflowNotFoundError — строки с таким id у владельца нет.
        """
        query = (
            self._query()
            .add(
                """
                update {schema}.workflows set
                    draft_spec     = null,
                    draft_layout   = null,
                    draft_revision = draft_revision + 1
                where 1=1
                    and id = %(id)s
                    and user_id = %(user_id)s
                returning
                    {workflow_columns}
                """,
                id=workflow_id,
                user_id=user_id,
            )
            .build()
        )
        row = await self._row(query, "clear_draft")
        if row is None:
            msg = (
                f"workflow #{workflow_id} of user {user_id} not found in "
                f"{self.schema}.workflows: draft not cleared"
            )
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredWorkflow, row)

    async def list_for(self, user_id: UUID) -> Sequence[StoredWorkflow]:
        query = (
            self._query()
            .add(
                """
                select
                    {workflow_columns}
                from
                    {schema}.workflows
                where
                    user_id = %(user_id)s
                order by
                    name
                """,
                user_id=user_id,
            )
            .build()
        )
        rows = await self._rows(query, "list")

        return self._parse_all(StoredWorkflow, rows)

    async def delete(self, user_id: UUID, workflow_id: UUID) -> bool:
        """Удаляет определение владельца; False — такого не было."""
        query = (
            self._query()
            .add(
                """
                delete from {schema}.workflows
                where
                    id = %(id)s
                    and user_id = %(user_id)s
                """,
                id=workflow_id,
                user_id=user_id,
            )
            .build()
        )
        removed = await self._execute(query, "delete")

        return removed > 0

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
        query = (
            self._query()
            .add(
                """
                insert into {schema}.workflow_runs (
                    id, workflow_id, user_id, initiator,
                    profile, status, state, instance
                )
                values (
                    %(id)s, %(workflow_id)s, %(user_id)s, %(initiator)s, %(profile)s,
                    %(status)s, %(state)s, %(instance)s
                )
                returning
                    {run_columns}
                """,
                id=run_id,
                workflow_id=workflow_id,
                user_id=user_id,
                initiator=Jsonb(dict(initiator)),
                profile=profile,
                status=state.status.value,
                state=Jsonb(state.persisted()),
                instance=instance,
            )
            .build()
        )
        row = self._returning(
            await self._row(query, "start run"),
            f"insert of run {run_id} into workflow_runs",
        )

        return self._parse(StoredRun, row)

    async def update_run(self, run_id: UUID, state: RunState) -> None:
        """Свежий снимок состояния; завершённый запуск получает finished_at.

        Ошибки:
        WorkflowNotFoundError — запуска с таким id нет.
        """
        query = (
            self._query()
            .add(
                """
                update {schema}.workflow_runs set
                    status      = %(status)s,
                    state       = %(state)s,
                    finished_at = case
                        when %(terminal)s then coalesce(finished_at, now())
                        else finished_at
                    end
                where
                    id = %(id)s
                """,
                id=run_id,
                status=state.status.value,
                state=Jsonb(state.persisted()),
                terminal=state.status.terminal,
            )
            .build()
        )
        touched = await self._execute(query, "update run")
        if touched == 0:
            msg = (
                f"workflow: run {run_id} not found in "
                f"{self.schema}.workflow_runs: state {state.status.value} "
                "not written"
            )
            raise WorkflowNotFoundError(msg)

    async def get_run(self, user_id: UUID, run_id: UUID) -> StoredRun:
        query = (
            self._query()
            .add(
                """
                select
                    {run_columns}
                from
                    {schema}.workflow_runs
                where 1=1
                    and id = %(id)s
                    and user_id = %(user_id)s
                """,
                id=run_id,
                user_id=user_id,
            )
            .build()
        )
        row = await self._row(query, "get run")
        if row is None:
            msg = (
                f"workflow: run {run_id} of user {user_id} not found in "
                f"{self.schema}.workflow_runs"
            )
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredRun, row)

    async def run_by_id(self, run_id: UUID) -> StoredRun:
        """Запуск по id без владельца: для получателей шины после проверки подписки."""
        query = (
            self._query()
            .add(
                """
                select
                    {run_columns}
                from
                    {schema}.workflow_runs
                where
                    id = %(id)s
                """,
                id=run_id,
            )
            .build()
        )
        row = await self._row(query, "get run")
        if row is None:
            msg = f"workflow: run {run_id} not found in {self.schema}.workflow_runs"
            raise WorkflowNotFoundError(msg)

        return self._parse(StoredRun, row)

    async def list_runs(self, user_id: UUID, limit: int) -> Sequence[StoredRun]:
        query = (
            self._query()
            .add(
                """
                select
                    {run_columns}
                from
                    {schema}.workflow_runs
                where
                    user_id = %(user_id)s
                order by
                    started_at desc
                limit %(limit)s
                """,
                user_id=user_id,
                limit=limit,
            )
            .build()
        )
        rows = await self._rows(query, "list runs")

        return self._parse_all(StoredRun, rows)

    async def running(self) -> Sequence[StoredRun]:
        """Незавершённые запуски всех инстансов: их сверяет с блокировками сторож."""
        query = (
            self._query()
            .add(
                """
                select
                    {run_columns}
                from
                    {schema}.workflow_runs
                where
                    status = any(%(statuses)s)
                order by
                    started_at
                """,
                statuses=[RunStatus.PENDING.value, RunStatus.RUNNING.value],
            )
            .build()
        )
        rows = await self._rows(query, "list running")

        return self._parse_all(StoredRun, rows)

    async def orphans_of(self, instance: str) -> Sequence[StoredRun]:
        """Незавершённые запуски этого инстанса: после перезапуска их никто не ведёт."""
        query = (
            self._query()
            .add(
                """
                select
                    {run_columns}
                from
                    {schema}.workflow_runs
                where 1=1
                    and instance = %(instance)s
                    and status = any(%(statuses)s)
                order by
                    started_at
                """,
                instance=instance,
                statuses=[RunStatus.PENDING.value, RunStatus.RUNNING.value],
            )
            .build()
        )
        rows = await self._rows(query, "list orphans")

        return self._parse_all(StoredRun, rows)

    def _tools_of(self, spec: WorkflowSpec) -> list[str]:
        names: list[str] = []
        for task in spec.tasks.values():
            if task.tool in names:
                continue

            names.append(task.tool)

        return sorted(names)
