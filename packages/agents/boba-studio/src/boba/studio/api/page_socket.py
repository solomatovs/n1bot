"""Socket.io API страницы: состояние шины и события области пользователя.

Подключение авторизуется cookie входа; сокет садится в комнату пользователя
и получает изменения его лент (user_event), все вкладки получают состояние
слушателя шины (bus_state). Подписка на запуск workflow отключена: subscribe
отвечает событием refused. REST остаётся источником истины, сокет — доставка.

Ошибки: своих не выпускает; отказ подключения — ConnectionRefusedError
socket.io, отказ подписки — событие `refused` с причиной.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import socketio

from boba.chat.profiles import ChatProfiles
from boba.identity.context import Scope, Subject
from boba.identity.errors import BaseError
from boba.messaging import Envelope, MessageBus, Unsubscribe
from boba.runtime.bus import BusWatch, ListenerState
from boba.runtime.config import StudioPath
from boba.runtime.refresh import LiveSessions, LiveToken
from boba.studio.api.auth import ApiAuth, SocketSignIn

__all__ = [
    "PageNamespace",
    "PageSocket",
    "PageSocketEvent",
    "SocketAuthenticator",
    "StudioSessions",
]

logger = logging.getLogger(__name__)

BusSource = Callable[[], MessageBus]
"""Шина процесса; зовётся на подключение."""

BusWatchSource = Callable[[], BusWatch]
"""Слушатель шины процесса; зовётся на подключение."""

SocketAuthenticator = Callable[[dict[str, Any]], Awaitable[SocketSignIn | None]]
"""WSGI environ подключения -> пользователь входа; None — cookie негодна."""


class PageSocketEvent(StrEnum):
    """События namespace страницы."""

    SUBSCRIBE = "subscribe"
    REFUSED = "refused"
    BUS_STATE = "bus_state"
    USER_EVENT = "user_event"


class UserRoom:
    """Имя комнаты пользователя: в неё приходят изменения его лент."""

    PREFIX: ClassVar[str] = "user:"

    @classmethod
    def of(cls, user_id: UUID) -> str:
        return f"{cls.PREFIX}{user_id}"


class StudioSessions(LiveSessions):
    """Реестр подключённых сокетов страницы: чей сокет и каким токеном вошёл."""

    def __init__(self) -> None:
        self._live: dict[str, LiveToken] = {}

    def attach(self, sid: str, live: LiveToken) -> None:
        self._live[sid] = live

    def detach(self, sid: str) -> None:
        self._live.pop(sid, None)

    def live_tokens(self) -> list[LiveToken]:
        return list(self._live.values())


class PageNamespace(socketio.AsyncNamespace):
    """Namespace socket.io страницы: сажает сокет в комнату пользователя, шлёт
    туда события его области и всем — состояние слушателя шины для лампочки.
    """

    NAME: ClassVar[str] = "/workflow"

    RUNS_DISABLED: ClassVar[str] = (
        "workflow runs are disabled: the workflow engine was removed"
    )

    def __init__(
        self,
        bus: BusSource,
        profiles: ChatProfiles,
        authenticate: SocketAuthenticator,
        bus_watch: BusWatchSource,
        sessions: StudioSessions,
    ) -> None:
        super().__init__(self.NAME)
        self._bus = bus
        self._profiles = profiles
        self._authenticate = authenticate
        self._bus_watch = bus_watch
        self._sessions = sessions
        self._watching: Unsubscribe | None = None
        self._subjects: dict[str, Subject] = {}
        self._user_leaves: dict[UUID, Unsubscribe] = {}
        self._user_rooms: dict[UUID, set[str]] = {}

    async def on_connect(self, sid: str, environ: dict[str, Any], auth: Any) -> None:
        signed = await self._authenticate(environ)
        if signed is None:
            msg = f"page socket connect for sid={sid}: no valid sign-in token"
            logger.warning(msg)
            raise ConnectionRefusedError(msg)

        user = signed.user
        try:
            identity = ApiAuth.resolve(user, None, self._profiles)
        except BaseError as exc:
            who = f"sid={sid} user {user.identifier!r}"
            msg = f"page socket connect for {who}: resolving profile failed: {exc}"
            logger.warning(msg)
            raise ConnectionRefusedError(msg) from exc

        self._subjects[sid] = identity.subject
        self._sessions.attach(
            sid,
            LiveToken(user_id=user.id, login=user.identifier, token=signed.token),
        )
        logger.info("page socket connect: sid=%s user=%s", sid, identity.subject.login)
        self._watch_bus()
        await self._join_user(sid, identity.subject.user_id)
        await self.emit(
            PageSocketEvent.BUS_STATE.value,
            self._bus_payload(self._bus_watch().state),
            to=sid,
        )

    async def _join_user(self, sid: str, user_id: UUID) -> None:
        """Сажает сокет в комнату пользователя и подписывает её на его область."""
        await self.enter_room(sid, UserRoom.of(user_id))
        self._user_rooms.setdefault(user_id, set()).add(sid)
        if user_id in self._user_leaves:
            return

        async def deliver(envelope: Envelope) -> None:
            logger.debug(
                "user event %s to %s", envelope.message.kind, UserRoom.of(user_id)
            )
            await self.emit(
                PageSocketEvent.USER_EVENT.value,
                envelope.message.model_dump(mode="json"),
                room=UserRoom.of(user_id),
            )

        scope = Scope.user(user_id)
        self._user_leaves[user_id] = self._bus().subscribe(scope, deliver)

    def _leave_user(self, user_id: UUID, sid: str) -> None:
        sids = self._user_rooms.get(user_id)
        if sids is None:
            return

        sids.discard(sid)
        if sids:
            return

        del self._user_rooms[user_id]
        leave = self._user_leaves.pop(user_id, None)
        if leave is not None:
            leave()

    def _watch_bus(self) -> None:
        """Подписывает namespace на смену состояния слушателя шины при первом
        подключении, чтобы рассылать её всем вкладкам.
        """
        if self._watching is not None:
            return

        def changed(state: ListenerState) -> None:
            payload = self._bus_payload(state)
            asyncio.get_running_loop().create_task(
                self.emit(PageSocketEvent.BUS_STATE.value, payload)
            )

        self._watching = self._bus_watch().watch(changed)

    @staticmethod
    def _bus_payload(state: ListenerState) -> dict[str, str]:
        return {"listener": state.value}

    async def on_disconnect(self, sid: str, reason: str = "") -> None:
        self._sessions.detach(sid)
        subject = self._subjects.pop(sid, None)
        if subject is not None:
            self._leave_user(subject.user_id, sid)

        logger.info("page socket disconnect: sid=%s reason=%s", sid, reason)

    async def on_subscribe(self, sid: str, data: Any) -> None:
        """Подписка на запуск workflow отключена: отказ событием refused."""
        logger.info("page socket refused subscription: sid=%s", sid)
        await self.emit(
            PageSocketEvent.REFUSED.value, {"reason": self.RUNS_DISABLED}, to=sid
        )


class PageSocket:
    """Сервер socket.io API и его ASGI-приложение; хост монтирует его под PATH."""

    PATH: ClassVar[str] = StudioPath.SOCKET

    @classmethod
    def build(cls, namespace: PageNamespace) -> socketio.ASGIApp:
        # origin по умолчанию: свой хост, с учётом X-Forwarded-Host/Proto за прокси;
        # пустой список отвергал любой Origin браузера, и живые статусы не доходили
        server = socketio.AsyncServer(async_mode="asgi")
        server.register_namespace(namespace)

        # путь проверяет Mount приложения; сам engine.io путь не сверяет
        return socketio.ASGIApp(socketio_server=server, socketio_path="")
