"""Namespace /workflow: подписка по входу, живые снимки, чужой запуск — отказ."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

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
from boba.identity.locks import LockMode, LockPurpose, MemoryLiveLocks, RunLocking
from boba.messaging import MemoryMessageBus, StreamAppended
from boba.runtime.bus import ListenerState, PgMessageBus, StaticBusWatch
from boba.runtime.config import AppName
from boba.runtime.locks import PgLiveLocks
from boba.studio.api.workflow_socket import WorkflowNamespace, WorkflowSocketEvent
from boba.toolrun.registry import ToolRegistry
from boba.workflow import RunStatus
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "workflow_socket_test"
SID = "sid-page-1"


def _bus_watch() -> StaticBusWatch:
    return StaticBusWatch(ListenerState.LISTENING)


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

    return WorkflowService(
        store,
        registry,
        "test:0",
        bus,
        RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
    )


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

    built = WorkflowNamespace(source, _profiles(app_config), authenticate, _bus_watch)
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
        user_id=UUID(user.id),
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
    bus_state = emitted.events[0]
    assert bus_state[0] == WorkflowSocketEvent.BUS_STATE.value
    assert bus_state[1] == {"listener": ListenerState.LISTENING.value}
    states = [e for e in emitted.events if e[0] == WorkflowSocketEvent.RUN_STATE.value]
    first = states[0]
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

    namespace = WorkflowNamespace(
        source, _profiles(app_config), authenticate, _bus_watch
    )
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


async def test_run_events_reach_a_namespace_on_another_instance(  # noqa: PLR0913
    store: WorkflowStore,
    app_config: AppConfig,
    test_database: str,
    pool: AsyncPostgresPool,
    user: PersistedUser,
    context: CallContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запуск ведёт инстанс A, страница подписана через инстанс B: снимки идут шиной."""
    cfg = app_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    cluster = app_config.cluster
    schema = app_config.data_layer.db_schema

    async def stand(name: str) -> tuple[PgMessageBus, PgLiveLocks]:
        bus = PgMessageBus(cfg, schema, name, AppName.STUDIO, cluster)
        bus._pool_ref = pool
        await bus.setup()
        await bus.start()
        locks = PgLiveLocks(cfg, schema, name, AppName.STUDIO, cluster)
        locks._pool_ref = pool
        await locks.register_instance()
        return bus, locks

    bus_a, locks_a = await stand("node1-chainlit")
    bus_b, locks_b = await stand("node2-studio")
    probe = Probe()

    async def registry() -> ToolRegistry:
        return _registry(probe, ["*"], profile=_profile(app_config))

    holder = WorkflowService(
        store,
        registry,
        "node1-chainlit",
        bus_a,
        RunLocking(locks=locks_a, heartbeat_sec=1.0),
    )
    viewer = WorkflowService(
        store,
        registry,
        "node2-studio",
        bus_b,
        RunLocking(locks=locks_b, heartbeat_sec=1.0),
    )

    async def source() -> WorkflowService:
        return viewer

    async def authenticate(environ: dict[str, Any]) -> AuthenticatedUser | None:
        return ChainlitUsers.of(user)

    async def room_noop(*args: Any, **kwargs: Any) -> None:
        return None

    emitted = Emitted()
    namespace = WorkflowNamespace(
        source, _profiles(app_config), authenticate, _bus_watch
    )
    monkeypatch.setattr(namespace, "emit", emitted.emit)
    monkeypatch.setattr(namespace, "enter_room", room_noop)
    monkeypatch.setattr(namespace, "leave_room", room_noop)

    try:
        await namespace.on_connect(SID, {"signed": True}, None)
        stored = await holder.save(context.subject, SPEC, {})
        run_id = holder.new_run_id()
        started = await holder.start(context, stored, run_id)
        await namespace.on_subscribe(SID, {"run_id": str(run_id)})

        outcome = await asyncio.wait_for(holder.execute(context, started), 10)
        assert outcome.state.status is RunStatus.DONE

        deadline = asyncio.get_running_loop().time() + 5
        while "done" not in emitted.states():
            assert asyncio.get_running_loop().time() < deadline, emitted.states()
            await asyncio.sleep(0.05)

        assert "running" in emitted.states()
        assert emitted.states()[-1] == "done"
    finally:
        await bus_a.stop()
        await bus_b.stop()


async def test_run_start_reaches_the_user_room(
    namespace: tuple[WorkflowNamespace, Emitted],
    service: WorkflowService,
    context: CallContext,
) -> None:
    """Лента пользователя: старт запуска приходит событием user_event в его комнату."""
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    stored = await service.save(context.subject, SPEC, {})
    run_id = service.new_run_id()
    started = await service.start(context, stored, run_id)
    await asyncio.wait_for(service.execute(context, started), 10)

    events = [e for e in emitted.events if e[0] == WorkflowSocketEvent.USER_EVENT.value]
    kinds = [e[1]["kind"] for e in events]
    assert "workflow_changed" in kinds
    listed = [e[1] for e in events if e[1]["kind"] == "run_list_changed"]
    assert listed[0]["status"] in ("pending", "running")
    assert listed[-1]["status"] == "done"
    assert listed[-1]["run_id"] == str(run_id)
    assert listed[-1]["workflow_name"] == stored.name
    assert all(e[2] == f"user:{context.subject.user_id}" for e in events)


async def test_user_events_reach_the_room_over_postgres(  # noqa: PLR0913
    store: WorkflowStore,
    app_config: AppConfig,
    test_database: str,
    pool: AsyncPostgresPool,
    user: PersistedUser,
    context: CallContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Лента пользователя через настоящую шину: сохранение workflow доходит в
    комнату.
    """
    cfg = app_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    bus = PgMessageBus(
        cfg,
        app_config.data_layer.db_schema,
        "node1-studio",
        AppName.STUDIO,
        app_config.cluster,
    )
    bus._pool_ref = pool
    await bus.setup()
    await bus.start()
    probe = Probe()

    async def registry() -> ToolRegistry:
        return _registry(probe, ["*"], profile=_profile(app_config))

    locks = PgLiveLocks(
        cfg,
        app_config.data_layer.db_schema,
        "node1-studio",
        AppName.STUDIO,
        app_config.cluster,
    )
    locks._pool_ref = pool
    await locks.register_instance()
    service = WorkflowService(
        store, registry, "node1-studio", bus, RunLocking(locks=locks, heartbeat_sec=1.0)
    )

    async def source() -> WorkflowService:
        return service

    async def authenticate(environ: dict[str, Any]) -> AuthenticatedUser | None:
        return ChainlitUsers.of(user)

    async def room_noop(*args: Any, **kwargs: Any) -> None:
        return None

    emitted = Emitted()
    namespace = WorkflowNamespace(
        source, _profiles(app_config), authenticate, _bus_watch
    )
    monkeypatch.setattr(namespace, "emit", emitted.emit)
    monkeypatch.setattr(namespace, "enter_room", room_noop)
    monkeypatch.setattr(namespace, "leave_room", room_noop)
    try:
        await namespace.on_connect(SID, {"signed": True}, None)
        await service.save(context.subject, SPEC, {})

        deadline = asyncio.get_running_loop().time() + 5
        while not [
            e for e in emitted.events if e[0] == WorkflowSocketEvent.USER_EVENT.value
        ]:
            assert asyncio.get_running_loop().time() < deadline, emitted.events
            await asyncio.sleep(0.05)

        events = [
            e for e in emitted.events if e[0] == WorkflowSocketEvent.USER_EVENT.value
        ]
        assert events[0][1]["kind"] == "workflow_changed"
    finally:
        await bus.stop()


async def test_stream_events_reach_the_run_room(
    namespace: tuple[WorkflowNamespace, Emitted],
    service: WorkflowService,
    context: CallContext,
    bus: MemoryMessageBus,
) -> None:
    """Рост журнала стадии приходит подписчикам запуска событием stream_event с полями
    сообщения и id запуска.
    """
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    stored = await service.save(context.subject, SPEC, {})
    run_id = service.new_run_id()
    started = await service.start(context, stored, run_id)
    await built.on_subscribe(SID, {"run_id": str(run_id)})
    await asyncio.wait_for(service.execute(context, started), 10)

    # запуск отпустил область: публикуем от имени нового держателя
    scope = Scope.workflow(run_id)
    lock = await service.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1))
    try:
        message = StreamAppended(
            call_id="call-1", channel="tool_stdout", size=12, closed=False, note=""
        )
        await bus.publish(scope, message, lock.token)
    finally:
        await service.locks.release(lock.token)

    deadline = asyncio.get_running_loop().time() + 5
    while not _stream_events(emitted):
        assert asyncio.get_running_loop().time() < deadline, emitted.events
        await asyncio.sleep(0.05)

    event, data, room = _stream_events(emitted)[0]
    assert event == WorkflowSocketEvent.STREAM_EVENT.value
    assert room == f"run:{run_id}"
    assert data == {
        "run_id": str(run_id),
        "call_id": "call-1",
        "channel": "tool_stdout",
        "size": 12,
        "closed": False,
        "note": "",
    }


def _stream_events(emitted: Emitted) -> list[tuple[str, Any, str]]:
    found: list[tuple[str, Any, str]] = []
    for item in emitted.events:
        if item[0] != WorkflowSocketEvent.STREAM_EVENT.value:
            continue

        found.append(item)

    return found
