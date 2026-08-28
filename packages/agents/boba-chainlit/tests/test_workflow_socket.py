"""Namespace /workflow: подписка по входу, живые снимки, чужой запуск — отказ."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
import socketio
from chainlit.user import PersistedUser
from conftest import ChainlitUsers, Seed, use_context
from psycopg import sql
from test_tool_api import _profile, _profiles, _roles
from test_workflow_service import ROLE, Probe, _registry

from boba.chainlit.infra.config import AppConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import AuthenticatedUser
from boba.identity.context import CallContext, Scope
from boba.messaging import MemoryMessageBus
from boba.studio.api.workflow_socket import WorkflowNamespace, WorkflowSocketEvent
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "workflow_socket_test"
SID = "sid-page-1"

SPEC = """
name: socket-flow
tasks:
  a: {tool: slow, args: {label: a, delay: 0.2}}
  b: {tool: slow, args: {label: b, delay: 0.2}}
edges:
  - a -> b
"""


class Emitted:
    """Что namespace отправил бы клиентам: событие, данные, адресат."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any, str]] = []

    async def emit(self, event: str, data: Any = None, **kwargs: Any) -> None:
        target = kwargs.get("to") or kwargs.get("room") or ""
        self.events.append((event, data, str(target)))

    def states(self) -> list[str]:
        statuses: list[str] = []
        for event, data, _target in self.events:
            if event == WorkflowSocketEvent.RUN_STATE.value:
                statuses.append(str(data["status"]))

        return statuses


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
def bus() -> MemoryMessageBus:
    return MemoryMessageBus("test:0")


@pytest.fixture
def service(
    store: WorkflowStore, app_config: AppConfig, bus: MemoryMessageBus
) -> WorkflowService:
    probe = Probe()

    async def registry() -> ToolRegistry:
        return _registry(probe, ["*"], profile=_profile(app_config))

    return WorkflowService(store, registry, "test:0", bus)


@pytest.fixture
def user(seeded: Seed, app_config: AppConfig) -> PersistedUser:
    seeded.user.metadata = {"roles": [*_roles(app_config), ROLE]}
    return seeded.user


@pytest.fixture
def namespace(
    service: WorkflowService,
    app_config: AppConfig,
    user: PersistedUser,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkflowNamespace, Emitted]:
    async def source() -> WorkflowService:
        return service

    async def authenticate(environ: dict[str, Any]) -> AuthenticatedUser | None:
        if environ.get("signed"):
            return ChainlitUsers.of(user)

        return None

    built = WorkflowNamespace(source, _profiles(app_config), authenticate)
    socketio.AsyncServer(async_mode="asgi").register_namespace(built)

    # живого сокета нет: комнаты — забота socket.io, здесь их не проверяем
    async def room_noop(sid: str, room: str, namespace: str | None = None) -> None:
        return None

    monkeypatch.setattr(built, "enter_room", room_noop)
    monkeypatch.setattr(built, "leave_room", room_noop)
    emitted = Emitted()
    monkeypatch.setattr(built, "emit", emitted.emit)
    return built, emitted


@pytest.fixture
def context(
    monkeypatch: pytest.MonkeyPatch, user: PersistedUser, app_config: AppConfig
) -> CallContext:
    return use_context(
        monkeypatch,
        thread_id="socket-thread",
        user_id=int(user.id),
        roles=[*_roles(app_config), ROLE],
        profile=_profile(app_config),
    )


async def test_unsigned_connection_is_refused(
    namespace: tuple[WorkflowNamespace, Emitted],
) -> None:
    built, _emitted = namespace
    with pytest.raises(ConnectionRefusedError):
        await built.on_connect(SID, {}, None)


async def test_subscription_streams_snapshots(
    namespace: tuple[WorkflowNamespace, Emitted],
    service: WorkflowService,
    context: CallContext,
    bus: MemoryMessageBus,
) -> None:
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    stored = await service.save(context.subject, SPEC, {})
    run_id = service.new_run_id()
    started = await service.start(context, stored, run_id)
    running = asyncio.create_task(service.execute(context, started))

    await built.on_subscribe(SID, {"run_id": str(run_id)})
    first = emitted.events[0]
    assert first[0] == WorkflowSocketEvent.RUN_STATE.value
    assert first[2] == SID
    assert first[1]["run_id"] == str(run_id)

    await asyncio.wait_for(running, 10)
    statuses = emitted.states()
    assert statuses[-1] == "done"
    assert "running" in statuses
    assert bus.listeners_of(Scope.workflow(run_id)) == 1

    await built.on_unsubscribe(SID, {"run_id": str(run_id)})
    assert bus.listeners_of(Scope.workflow(run_id)) == 0


async def test_foreign_run_is_refused(
    namespace: tuple[WorkflowNamespace, Emitted],
) -> None:
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    await built.on_subscribe(SID, {"run_id": str(uuid4())})

    assert emitted.events[-1][0] == WorkflowSocketEvent.REFUSED.value
    assert "not found" in str(emitted.events[-1][1]["reason"])


async def test_disconnect_drops_listeners(
    namespace: tuple[WorkflowNamespace, Emitted],
    service: WorkflowService,
    context: CallContext,
    bus: MemoryMessageBus,
) -> None:
    built, _emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)
    stored = await service.save(context.subject, SPEC, {})
    run_id = service.new_run_id()
    started = await service.start(context, stored, run_id)
    running = asyncio.create_task(service.execute(context, started))

    await built.on_subscribe(SID, {"run_id": str(run_id)})
    await built.on_disconnect(SID)
    assert bus.listeners_of(Scope.workflow(run_id)) == 0

    await asyncio.wait_for(running, 10)


async def test_websocket_handshake_accepts_the_browser_origin_behind_a_proxy(
    service: WorkflowService, app_config: AppConfig, user: PersistedUser
) -> None:
    """Браузер шлёт Origin своего хоста; за прокси хост приходит в X-Forwarded-*."""
    import socket
    import threading

    import uvicorn
    from fastapi import FastAPI
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    from websockets.typing import Origin

    from boba.studio.api.workflow_socket import WorkflowSocket

    async def source() -> WorkflowService:
        return service

    async def authenticate(environ: dict[str, Any]) -> AuthenticatedUser | None:
        return ChainlitUsers.of(user)

    namespace = WorkflowNamespace(source, _profiles(app_config), authenticate)
    app = FastAPI()
    app.mount("/socket.io", WorkflowSocket.build(namespace))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)

        url = f"ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket"
        forwarded = [
            ("x-forwarded-proto", "https"),
            ("x-forwarded-host", "loshara.com"),
        ]
        async with connect(
            url, origin=Origin("https://loshara.com"), additional_headers=forwarded
        ) as ws:
            opened = await asyncio.wait_for(ws.recv(), 5)
            assert str(opened).startswith("0{")

        with pytest.raises(InvalidStatus, match="403"):
            async with connect(url, origin=Origin("https://evil.test")):
                pass
    finally:
        server.should_exit = True
        thread.join(10)
