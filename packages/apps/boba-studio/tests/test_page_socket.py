"""Сокет страницы: вход по cookie, состояние шины, события пользователя; подписка
на запуск workflow отключена, как и его REST."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import socketio
from fastapi import HTTPException
from studio_stand import StandProfiles

from boba.identity.api import AuthenticatedUser
from boba.identity.context import Scope
from boba.identity.locks import LockToken
from boba.messaging import ChangeAction, ConnectionsChanged, MemoryMessageBus
from boba.messaging.bus import ListenerState, StaticBusWatch
from boba.runtime.config import StudioRuntimeConfig
from boba.stand_core.auth import StubAuthenticator
from boba.studio.api.auth import SocketSignIn
from boba.studio.api.page_socket import PageNamespace, PageSocketEvent, StudioSessions
from boba.studio.api.workflows import WorkflowApi

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SID = "sid-page-1"


def _bus_watch() -> StaticBusWatch:
    return StaticBusWatch(ListenerState.LISTENING)


class Emitted:
    """Что namespace отправил бы клиентам: событие, данные, адресат."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any, str]] = []

    async def emit(self, event: str, data: Any = None, **kwargs: Any) -> None:
        target = kwargs.get("to")
        if not target:
            target = kwargs.get("room")
        if not target:
            target = ""

        self.events.append((event, data, str(target)))

    def of(self, event: PageSocketEvent) -> list[tuple[str, Any, str]]:
        found: list[tuple[str, Any, str]] = []
        for item in self.events:
            if item[0] == event.value:
                found.append(item)

        return found


@pytest.fixture
def bus() -> MemoryMessageBus:
    return MemoryMessageBus("test:0")


@pytest.fixture
def user(studio_config: StudioRuntimeConfig) -> AuthenticatedUser:
    return StandProfiles.user(studio_config)


@pytest.fixture
def sessions() -> StudioSessions:
    return StudioSessions()


@pytest.fixture
def namespace(
    bus: MemoryMessageBus,
    studio_config: StudioRuntimeConfig,
    user: AuthenticatedUser,
    sessions: StudioSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PageNamespace, Emitted]:
    async def authenticate(environ: dict[str, Any]) -> SocketSignIn | None:
        if environ.get("signed"):
            return SocketSignIn(user=user, token=StubAuthenticator.TOKEN)

        return None

    built = PageNamespace(
        lambda: bus,
        StandProfiles.profiles(studio_config),
        authenticate,
        _bus_watch,
        sessions,
    )
    socketio.AsyncServer(async_mode="asgi").register_namespace(built)

    # живого сокета нет: комнаты — забота socket.io, здесь их не проверяем
    async def room_noop(sid: str, room: str, namespace: str | None = None) -> None:
        return None

    monkeypatch.setattr(built, "enter_room", room_noop)
    monkeypatch.setattr(built, "leave_room", room_noop)

    emitted = Emitted()
    monkeypatch.setattr(built, "emit", emitted.emit)

    return built, emitted


async def test_unsigned_connection_is_refused(
    namespace: tuple[PageNamespace, Emitted],
) -> None:
    built, _emitted = namespace

    with pytest.raises(ConnectionRefusedError):
        await built.on_connect(SID, {}, None)


async def test_connection_gets_the_bus_state_and_a_session(
    namespace: tuple[PageNamespace, Emitted],
    sessions: StudioSessions,
    user: AuthenticatedUser,
) -> None:
    built, emitted = namespace

    await built.on_connect(SID, {"signed": True}, None)

    state = emitted.events[0]
    assert state[0] == PageSocketEvent.BUS_STATE.value
    assert state[1] == {"listener": ListenerState.LISTENING.value}
    assert state[2] == SID
    assert [live.user_id for live in sessions.live_tokens()] == [user.id]

    await built.on_disconnect(SID)

    assert sessions.live_tokens() == []


async def test_user_event_reaches_the_user_room(
    namespace: tuple[PageNamespace, Emitted],
    bus: MemoryMessageBus,
    user: AuthenticatedUser,
) -> None:
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    message = ConnectionsChanged(
        connection_id=uuid4(), name="main", action=ChangeAction.CREATED
    )
    await bus.publish(Scope.user(user.id), message, LockToken.local())

    events = emitted.of(PageSocketEvent.USER_EVENT)
    assert len(events) == 1
    assert events[0][1]["kind"] == "connections_changed"
    assert events[0][2] == f"user:{user.id}"


async def test_disconnected_user_gets_no_events(
    namespace: tuple[PageNamespace, Emitted],
    bus: MemoryMessageBus,
    user: AuthenticatedUser,
) -> None:
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)
    await built.on_disconnect(SID)

    message = ConnectionsChanged(
        connection_id=uuid4(), name="main", action=ChangeAction.CREATED
    )
    await bus.publish(Scope.user(user.id), message, LockToken.local())

    assert emitted.of(PageSocketEvent.USER_EVENT) == []


async def test_run_subscription_is_refused(
    namespace: tuple[PageNamespace, Emitted],
) -> None:
    built, emitted = namespace
    await built.on_connect(SID, {"signed": True}, None)

    await built.on_subscribe(SID, {"run_id": str(uuid4())})

    refused = emitted.of(PageSocketEvent.REFUSED)
    assert len(refused) == 1
    assert "workflow runs are disabled" in refused[0][1]["reason"]
    assert refused[0][2] == SID


async def test_workflow_rest_is_disabled(user: AuthenticatedUser) -> None:
    with pytest.raises(HTTPException) as caught:
        await WorkflowApi().disabled(user)

    assert caught.value.status_code == 501
    assert "workflow runs are disabled" in str(caught.value.detail)
