"""Хранилище workflow на живом postgres: определения, запуски, изоляция владельца."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from psycopg import sql

from boba.chainlit.workflow.store import (
    WorkflowConfig,
    WorkflowNotFoundError,
    WorkflowStore,
)
from boba.db.postgres import AsyncPostgresPool
from boba.workflow import (
    RunState,
    RunStatus,
    Stage,
    TaskState,
    TaskStatus,
    WorkflowGraph,
    WorkflowSpec,
)

pytestmark = pytest.mark.anyio

SCHEMA = "workflow_test"
OWNER = 7
STRANGER = 8

SPEC = """
name: pg-to-ch
description: copy batch
tasks:
  dump:
    tool: bash
    ports: {out: write}
    args: {command: "psql > $out"}
  load:
    tool: bash
    ports: {src: read}
    args: {command: "clickhouse-client < $src"}
  ids:
    tool: pg_query
    args: {query: "select id from batches"}
edges:
  - dump.out -> load.src
"""


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> WorkflowStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    built = WorkflowStore(WorkflowConfig(enable=True, db_schema=SCHEMA), pool)
    await built.setup()
    return built


def _spec() -> WorkflowSpec:
    return WorkflowSpec.parse_yaml(SPEC)


def _state(spec: WorkflowSpec, status: RunStatus) -> RunState:
    tasks: dict[str, TaskState] = {}
    for name in spec.tasks:
        tasks[name] = TaskState(status=TaskStatus.PENDING)

    if status is RunStatus.DONE:
        for name in spec.tasks:
            tasks[name] = TaskState(
                status=TaskStatus.DONE,
                call_id=f"call-{name}",
                started_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
                finished_at=datetime(2026, 8, 25, 12, 0, 5, tzinfo=UTC),
            )

    graph = WorkflowGraph(
        spec=spec,
        stages=(
            Stage(id="stage:dump", tasks=("dump", "load")),
            Stage(id="stage:ids", tasks=("ids",)),
        ),
    )
    return RunState(graph=graph, status=status, tasks=tasks)


async def test_setup_is_idempotent(store: WorkflowStore) -> None:
    await store.setup()
    await store.setup()


async def test_save_get_list_and_tools(store: WorkflowStore) -> None:
    saved = await store.save(OWNER, _spec(), {"dump": {"x": 1, "y": 2}})

    if saved.name != "pg-to-ch" or saved.user_id != OWNER:
        raise AssertionError(saved)
    if saved.tools != ("bash", "pg_query"):
        raise AssertionError(saved.tools)
    if saved.layout != {"dump": {"x": 1, "y": 2}}:
        raise AssertionError(saved.layout)

    if WorkflowSpec.parse_yaml(saved.spec) != _spec():
        raise AssertionError("spec must survive the roundtrip as yaml")

    by_id = await store.get(OWNER, saved.id)
    by_name = await store.get_by_name(OWNER, "pg-to-ch")
    if by_id != saved or by_name != saved:
        raise AssertionError("get by id and by name return the saved row")

    listed = await store.list_for(OWNER)
    if [w.id for w in listed] != [saved.id]:
        raise AssertionError(listed)


async def test_save_again_upserts_by_owner_and_name(store: WorkflowStore) -> None:
    first = await store.save(OWNER, _spec(), {})
    again = await store.save(OWNER, _spec(), {"dump": {"x": 5}})

    if again.id != first.id:
        raise AssertionError("same owner and name keep the row")
    if again.layout != {"dump": {"x": 5}}:
        raise AssertionError(again.layout)
    if again.updated_at < first.updated_at:
        raise AssertionError("updated_at moves forward")

    other = await store.save(STRANGER, _spec(), {})
    if other.id == first.id:
        raise AssertionError("another owner gets an own row with the same name")


async def test_owner_isolation(store: WorkflowStore) -> None:
    saved = await store.save(OWNER, _spec(), {})

    with pytest.raises(WorkflowNotFoundError):
        await store.get(STRANGER, saved.id)

    if await store.list_for(STRANGER) != []:
        raise AssertionError("stranger sees nothing")
    if await store.delete(STRANGER, saved.id):
        raise AssertionError("stranger cannot delete")
    if not await store.delete(OWNER, saved.id):
        raise AssertionError("owner deletes")


async def test_run_lifecycle(store: WorkflowStore) -> None:
    spec = _spec()
    saved = await store.save(OWNER, spec, {})
    run_id = uuid4()

    started = await store.start_run(
        run_id,
        saved.id,
        OWNER,
        {"kind": "human", "via": "api"},
        "general",
        _state(spec, RunStatus.RUNNING),
        "node-1",
    )
    if started.status is not RunStatus.RUNNING or started.finished_at is not None:
        raise AssertionError(started)
    if started.initiator != {"kind": "human", "via": "api"}:
        raise AssertionError(started.initiator)
    if started.state.graph.spec != spec:
        raise AssertionError("the run keeps a snapshot of the spec")

    await store.update_run(run_id, _state(spec, RunStatus.DONE))

    finished = await store.get_run(OWNER, run_id)
    if finished.status is not RunStatus.DONE or finished.finished_at is None:
        raise AssertionError(finished)
    if finished.state.tasks["dump"].call_id != "call-dump":
        raise AssertionError(finished.state)

    listed = await store.list_runs(OWNER, limit=10)
    if [r.id for r in listed] != [run_id]:
        raise AssertionError(listed)

    with pytest.raises(WorkflowNotFoundError):
        await store.get_run(STRANGER, run_id)

    with pytest.raises(WorkflowNotFoundError):
        await store.update_run(uuid4(), _state(spec, RunStatus.DONE))


async def test_deleted_definition_keeps_its_runs(store: WorkflowStore) -> None:
    spec = _spec()
    saved = await store.save(OWNER, spec, {})
    run_id = uuid4()
    await store.start_run(
        run_id,
        saved.id,
        OWNER,
        {"kind": "human", "via": "page"},
        "general",
        _state(spec, RunStatus.RUNNING),
        "node-1",
    )

    await store.delete(OWNER, saved.id)

    orphan = await store.get_run(OWNER, run_id)
    if orphan.workflow_id is not None:
        raise AssertionError("the run outlives its definition without a link")


async def test_snapshot_is_independent_of_the_definition(store: WorkflowStore) -> None:
    spec = _spec()
    saved = await store.save(OWNER, spec, {})
    run_id = uuid4()
    await store.start_run(
        run_id,
        saved.id,
        OWNER,
        {"kind": "human", "via": "page"},
        "general",
        _state(spec, RunStatus.RUNNING),
        "node-1",
    )

    changed = spec.model_copy(update={"description": "changed later"})
    await store.save(OWNER, changed, {})

    run = await store.get_run(OWNER, run_id)
    if run.state.graph.spec.description != "copy batch":
        raise AssertionError("editing the definition must not touch the run")
