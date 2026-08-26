"""REST workflow через FastAPI: сохранение, запуск в фоне, опрос, остановка."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chainlit.user import PersistedUser
from conftest import NoThreads, Seed, StubAuthenticator, StubRefs
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from test_tool_api import _profile, _profiles, _roles
from test_workflow_service import ROLE, Probe, _registry

from boba.api.app import ApiApp
from boba.api.urls import ApiVersion, WorkflowUrl
from boba.chainlit.infra.api_auth import ChainlitUsers
from boba.chainlit.infra.config import AppConfig
from boba.db.postgres import AsyncPostgresPool
from boba.toolrun.registry import ToolRegistry
from boba.workflow.events import RunEvents
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
def user(seeded: Seed, app_config: AppConfig) -> PersistedUser:
    """Пользователь стенда с ролями конфига и ролью реестра зондов."""
    seeded.user.metadata = {"roles": [*_roles(app_config), ROLE]}
    return seeded.user


@pytest.fixture
def app(store: WorkflowStore, user: PersistedUser, app_config: AppConfig) -> FastAPI:
    probe = Probe()

    async def registry() -> ToolRegistry:
        return _registry(probe, ["*"], profile=_profile(app_config))

    service = WorkflowService(store, registry, "test:0", RunEvents())

    async def source() -> WorkflowService:
        return service

    return ApiApp.build(
        StubRefs.services(registry, source),
        StubAuthenticator(ChainlitUsers.of(user)),
        NoThreads.source,
        _profiles(app_config),
        StubAuthenticator.COOKIE,
    )


@pytest.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api",
        cookies=StubAuthenticator.cookies(),
    ) as c:
        yield c


def _profile_of(app_config: AppConfig) -> str:
    return _profile(app_config)


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


async def test_catalog_lists_tools(client: AsyncClient, app_config: AppConfig) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{WorkflowUrl.CATALOG}",
        params={"profile": _profile_of(app_config)},
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


async def test_validate_and_save(client: AsyncClient, app_config: AppConfig) -> None:
    profile = _profile_of(app_config)

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
    client: AsyncClient, app_config: AppConfig
) -> None:
    profile = _profile_of(app_config)
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

    missing = await client.post("/v1/workflows/999999/run", json={"profile": profile})
    assert missing.status_code == 404


async def test_stop_running(client: AsyncClient, app_config: AppConfig) -> None:
    profile = _profile_of(app_config)
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
    assert stopped.json() == {"stopped": True}

    run = await _finished(client, run_id, profile)
    assert run["status"] == "stopped"

    again = await client.post(
        f"/v1/workflow-runs/{run_id}/stop", json={"profile": profile}
    )
    assert again.json() == {"stopped": False}

    listed = await client.get(
        f"{ApiVersion.V1}{WorkflowUrl.RUNS}", params={"profile": profile}
    )
    assert [item["id"] for item in listed.json()] == [run_id]
