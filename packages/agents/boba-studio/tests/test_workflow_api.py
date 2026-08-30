"""REST workflow через FastAPI: сохранение, запуск в фоне, опрос, остановка."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from studio_stand import NoUsers, StandProfiles, StubAuthenticator, StubRefs

from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import ApiSubject, AuthenticatedUser
from boba.identity.locks import MemoryLiveLocks, RunLocking
from boba.messaging import MemoryMessageBus
from boba.runtime.config import StudioRuntimeConfig
from boba.stand.tools import PROBE_ROLE as ROLE
from boba.stand.tools import Probe
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.urls import ApiVersion, ToolCallUrl, WorkflowUrl
from boba.toolrun.registry import ToolRegistry
from boba.workflow import RunStatus, WorkflowSpec
from boba.workflow.records import StopOutcome
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "workflow_api_test"

SPEC = """
name: api-flow
tasks:
  first: {tool: echo, args: {text: hello}}
  second: {tool: echo, args: {text: "{{ first }} world"}}
edges:
  - first.result -> second.args.text
"""

LONG = """
name: api-long
tasks:
  wait: {tool: slow, args: {label: wait, delay: 30}}
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


@pytest.fixture
def user(studio_config: StudioRuntimeConfig) -> AuthenticatedUser:
    """Пользователь стенда с ролями конфига и ролью реестра зондов."""
    return StandProfiles.user(studio_config, [ROLE])


@pytest.fixture
def app(store: WorkflowStore, user: AuthenticatedUser, studio_config: StudioRuntimeConfig) -> FastAPI:
    probe = Probe()

    async def registry() -> ToolRegistry:
        return probe.registry(["*"], profile=StandProfiles.profile(studio_config))

    service = WorkflowService(
        store,
        registry,
        "test:0",
        MemoryMessageBus("test:0"),
        RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
    )

    async def source() -> WorkflowService:
        return service

    access = ApiAccess(
        StubAuthenticator(user),
        StubAuthenticator.COOKIE,
        NoUsers.source,
    )
    return ApiApp.build(
        StubRefs.services(registry, source), access, StandProfiles.profiles(studio_config), None
    )


@pytest.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api",
        cookies=StubAuthenticator.cookies(),
    ) as c:
        yield c


def _profile_of(studio_config: StudioRuntimeConfig) -> str:
    return StandProfiles.profile(studio_config)


async def _finished(client: AsyncClient, run_id: str, profile: str) -> dict[str, Any]:
    for _ in range(200):
        reply = await client.get(
            f"/v1/workflow-runs/{run_id}", params={"profile": profile}
        )
        assert reply.status_code == 200, reply.text
        run = reply.json()
        if run["status"] in ("done", "failed", "stopped"):
            return run

        await asyncio.sleep(0.02)

    raise AssertionError("the run never finished")


async def test_catalog_lists_tools(client: AsyncClient, studio_config: StudioRuntimeConfig) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{ToolCallUrl.CATALOG}",
        params={"profile": _profile_of(studio_config)},
    )

    assert reply.status_code == 200, reply.text
    catalog = reply.json()
    assert catalog["echo"]["availability"] == "available"
    assert catalog["canvas_open"]["availability"] == "chat_only"
    args = {arg["name"]: arg["required"] for arg in catalog["slow"]["args"]}
    assert args == {"label": True, "delay": True, "intent": False}
    views = {arg["name"]: arg["view"] for arg in catalog["slow"]["args"]}
    assert views["label"] == {
        "kind": "text",
        "placement": "body",
        "multiline": False,
        "placeholder": "",
    }
    assert views["delay"]["kind"] == "number"
    assert views["intent"] == {"kind": "intent", "placement": "header"}
    assert catalog["slow"]["results"] == []
    assert catalog["echo"]["results"] == ["text"]


async def test_validate_and_save(client: AsyncClient, studio_config: StudioRuntimeConfig) -> None:
    profile = _profile_of(studio_config)

    valid = await client.post(
        f"{ApiVersion.V1}{WorkflowUrl.VALIDATE}",
        json={"profile": profile, "spec": SPEC},
    )
    assert valid.status_code == 200, valid.text
    assert [stage["tasks"] for stage in valid.json()["graph"]["stages"]] == [
        ["first"],
        ["second"],
    ]

    broken = await client.post(
        f"{ApiVersion.V1}{WorkflowUrl.VALIDATE}",
        json={"profile": profile, "spec": "name: x\ntasks:\n  t: {tool: nope}\n"},
    )
    assert broken.status_code == 400
    assert "nope" in broken.json()["detail"]

    saved = await client.post(
        f"{ApiVersion.V1}{WorkflowUrl.WORKFLOWS}",
        json={"profile": profile, "spec": SPEC, "layout": {"first": [0, 0]}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["tools"] == ["echo"]

    listed = await client.get(
        f"{ApiVersion.V1}{WorkflowUrl.WORKFLOWS}", params={"profile": profile}
    )
    assert [item["name"] for item in listed.json()] == ["api-flow"]

    wrong_profile = await client.get(
        f"{ApiVersion.V1}{WorkflowUrl.WORKFLOWS}", params={"profile": "no-such-profile"}
    )
    assert wrong_profile.status_code == 403


async def test_run_in_background_and_poll(
    client: AsyncClient, studio_config: StudioRuntimeConfig
) -> None:
    profile = _profile_of(studio_config)
    saved = await client.post(
        f"{ApiVersion.V1}{WorkflowUrl.WORKFLOWS}",
        json={"profile": profile, "spec": SPEC},
    )
    workflow_id = saved.json()["id"]

    started = await client.post(
        f"/v1/workflows/{workflow_id}/run", json={"profile": profile}
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    run = await _finished(client, run_id, profile)
    assert run["status"] == "done"
    assert run["state"]["tasks"]["second"]["status"] == "done"
    assert run["initiator"] == {"kind": "human", "via": "api"}

    missing = await client.post(f"/v1/workflows/{uuid4()}/run", json={"profile": profile})
    assert missing.status_code == 404


async def test_stop_running(client: AsyncClient, studio_config: StudioRuntimeConfig) -> None:
    profile = _profile_of(studio_config)
    saved = await client.post(
        f"{ApiVersion.V1}{WorkflowUrl.WORKFLOWS}",
        json={"profile": profile, "spec": LONG},
    )
    started = await client.post(
        f"/v1/workflows/{saved.json()['id']}/run", json={"profile": profile}
    )
    run_id = started.json()["run_id"]

    for _ in range(100):
        reply = await client.get(
            f"/v1/workflow-runs/{run_id}", params={"profile": profile}
        )
        if reply.json()["status"] == "running":
            break

        await asyncio.sleep(0.02)

    stopped = await client.post(
        f"/v1/workflow-runs/{run_id}/stop", json={"profile": profile}
    )
    assert stopped.json() == {"outcome": "stopped"}

    run = await _finished(client, run_id, profile)
    assert run["status"] == "stopped"

    again = await client.post(
        f"/v1/workflow-runs/{run_id}/stop", json={"profile": profile}
    )
    assert again.json() == {"outcome": "finished"}

    listed = await client.get(
        f"{ApiVersion.V1}{WorkflowUrl.RUNS}", params={"profile": profile}
    )
    assert [item["id"] for item in listed.json()] == [run_id]


async def test_abandoned_runs_are_closed_on_startup_and_stop_is_honest(
    store: WorkflowStore, user: AuthenticatedUser, studio_config: StudioRuntimeConfig
) -> None:
    """Запуск без процесса: recover_orphans закрывает свои, stop чужого без держателя
    закрывает его как сироту.
    """
    probe = Probe()

    async def registry() -> ToolRegistry:
        return probe.registry(["*"], profile=StandProfiles.profile(studio_config))

    mine = WorkflowService(
        store,
        registry,
        "test:0",
        MemoryMessageBus("test:0"),
        RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
    )
    other = WorkflowService(
        store,
        registry,
        "other:1",
        MemoryMessageBus("other:1"),
        RunLocking(locks=MemoryLiveLocks("other:1", 20), heartbeat_sec=1.0),
    )
    signed = user
    assert signed is not None
    subject = ApiSubject.of(signed, StandProfiles.profile(studio_config)).subject
    saved = await store.save(subject.user_id, WorkflowSpec.parse_yaml(SPEC), {})
    graph = await mine.validate(subject, saved.spec)
    initial = mine.initial_state(graph)
    running = initial.model_copy(update={"status": RunStatus.RUNNING})

    orphan = await store.start_run(
        uuid4(), saved.id, subject.user_id, {}, subject.profile, running, "test:0"
    )
    foreign = await store.start_run(
        uuid4(), saved.id, subject.user_id, {}, subject.profile, running, "other:1"
    )

    assert await mine.recover_orphans() == 1
    closed = await store.get_run(subject.user_id, orphan.id)
    assert closed.state.status is RunStatus.FAILED
    assert (
        await store.get_run(subject.user_id, foreign.id)
    ).state.status is RunStatus.RUNNING

    # чужой запуск без живого держателя закрывается как сирота с любого инстанса
    assert await mine.stop(subject, foreign.id) is StopOutcome.STOPPED
    assert await other.stop(subject, foreign.id) is StopOutcome.FINISHED
    assert (
        await store.get_run(subject.user_id, foreign.id)
    ).state.status is RunStatus.FAILED
    assert await mine.stop(subject, orphan.id) is StopOutcome.FINISHED


async def test_draft_round_trip(client: AsyncClient, studio_config: StudioRuntimeConfig) -> None:
    """Черновик билдера: 404 до записи, PUT растит revision, DELETE снимает."""
    profile = _profile_of(studio_config)
    key = "new:0f3b2a10-1111-4222-8333-444455556666"

    missing = await client.get(
        f"/v1/workflows/drafts/{key}", params={"profile": profile}
    )
    assert missing.status_code == 404

    bad = await client.get("/v1/workflows/drafts/nope", params={"profile": profile})
    assert bad.status_code == 400

    body = {"spec": "name: draft\n", "layout": {"positions": {}}, "sid": "sid-1"}
    first = await client.put(
        f"/v1/workflows/drafts/{key}", params={"profile": profile}, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1

    second = await client.put(
        f"/v1/workflows/drafts/{key}", params={"profile": profile}, json=body
    )
    assert second.json()["revision"] == 2

    found = await client.get(f"/v1/workflows/drafts/{key}", params={"profile": profile})
    assert found.json()["spec"] == "name: draft\n"

    dropped = await client.delete(
        f"/v1/workflows/drafts/{key}", params={"profile": profile, "sid": "sid-1"}
    )
    assert dropped.json() == {"deleted": True}
    gone = await client.get(f"/v1/workflows/drafts/{key}", params={"profile": profile})
    assert gone.status_code == 404
