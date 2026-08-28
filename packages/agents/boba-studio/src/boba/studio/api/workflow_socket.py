"""Socket.io API: namespace /workflow с живыми снимками запусков странице.

Подключение авторизуется cookie входа; subscribe пускает в комнату запуска
после проверки владения и сразу шлёт текущий снимок, дальше снимки идут по
мере хода запуска. REST остаётся источником истины, сокет — доставка.

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
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.chat.profiles import ChatProfiles
from boba.identity.api import AuthenticatedUser
from boba.identity.context import Scope, Subject
from boba.identity.errors import RefusalError
from boba.messaging import Envelope, MessageKind, Unsubscribe
from boba.runtime.bus import BusWatch, ListenerState
from boba.runtime.config import StudioPath
from boba.studio.api.auth import ApiIdentity
from boba.workflow.events import RunSnapshot
from boba.workflow_engine.service import WorkflowService

__all__ = [
    "SocketAuthenticator",
    "WorkflowNamespace",
    "WorkflowSocket",
    "WorkflowSocketEvent",
]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[WorkflowService]]

BusWatchSource = Callable[[], BusWatch]
"""Слушатель шины процесса; зовётся на подключение."""

SocketAuthenticator = Callable[[dict[str, Any]], Awaitable[AuthenticatedUser | None]]
"""WSGI environ подключения -> пользователь входа; None — cookie негодна."""


class WorkflowSocketEvent(StrEnum):
    """События namespace /workflow."""

    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    RUN_STATE = "run_state"
    REFUSED = "refused"
    BUS_STATE = "bus_state"
    USER_EVENT = "user_event"


class Subscription(BaseModel):
    """Тело subscribe/unsubscribe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class RunRoom:
    """Имя комнаты запуска."""

    PREFIX: ClassVar[str] = "run:"

    @classmethod
    def of(cls, run_id: UUID) -> str:
        return f"{cls.PREFIX}{run_id}"


class UserRoom:
    """Имя комнаты пользователя: в неё приходят изменения его лент."""

    PREFIX: ClassVar[str] = "user:"

    @classmethod
    def of(cls, user_id: int) -> str:
        return f"{cls.PREFIX}{user_id}"


class WorkflowNamespace(socketio.AsyncNamespace):
    """Namespace socket.io страницы: пускает подписчиков в комнаты запусков, шлёт им
    снимки по сообщениям шины и состояние слушателя шины для лампочки.
    """

    NAME: ClassVar[str] = "/workflow"
    RUN_MESSAGES: ClassVar[frozenset[MessageKind]] = frozenset(
        {MessageKind.RUN_STATE_CHANGED, MessageKind.RUN_FINISHED}
    )

    def __init__(
        self,
        service: ServiceSource,
        profiles: ChatProfiles,
        authenticate: SocketAuthenticator,
        bus_watch: BusWatchSource,
    ) -> None:
        super().__init__(self.NAME)
        self._service = service
        self._profiles = profiles
        self._authenticate = authenticate
        self._bus_watch = bus_watch
        self._watching: Unsubscribe | None = None
        self._subjects: dict[str, Subject] = {}
        self._leaves: dict[UUID, Callable[[], None]] = {}
        self._rooms: dict[UUID, set[str]] = {}
        self._user_leaves: dict[int, Unsubscribe] = {}
        self._user_rooms: dict[int, set[str]] = {}

    async def on_connect(self, sid: str, environ: dict[str, Any], auth: Any) -> None:
        user = await self._authenticate(environ)
        try:
            identity = ApiIdentity.resolve(user, None, self._profiles)
        except HTTPException as exc:
            logger.warning("workflow socket refused: sid=%s %s", sid, exc.detail)
            raise ConnectionRefusedError(str(exc.detail)) from exc

        self._subjects[sid] = identity.subject
        logger.info(
            "workflow socket connect: sid=%s user=%s", sid, identity.subject.login
        )
        self._watch_bus()
        await self._join_user(sid, identity.subject.user_id)
        await self.emit(
            WorkflowSocketEvent.BUS_STATE.value,
            self._bus_payload(self._bus_watch().state),
            to=sid,
        )

    async def _join_user(self, sid: str, user_id: int) -> None:
        """Сажает сокет в комнату пользователя и подписывает её на его область."""
        await self.enter_room(sid, UserRoom.of(user_id))
        self._user_rooms.setdefault(user_id, set()).add(sid)
        if user_id in self._user_leaves:
            return

        service = await self._service()

        async def deliver(envelope: Envelope) -> None:
            logger.debug(
                "user event %s to %s", envelope.message.kind, UserRoom.of(user_id)
            )
            await self.emit(
                WorkflowSocketEvent.USER_EVENT.value,
                envelope.message.model_dump(mode="json"),
                room=UserRoom.of(user_id),
            )

        scope = Scope.user(user_id)
        self._user_leaves[user_id] = service.bus.subscribe(scope, deliver)

    def _leave_user(self, user_id: int, sid: str) -> None:
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
                self.emit(WorkflowSocketEvent.BUS_STATE.value, payload)
            )

        self._watching = self._bus_watch().watch(changed)

    @staticmethod
    def _bus_payload(state: ListenerState) -> dict[str, str]:
        return {"listener": state.value}

    async def on_disconnect(self, sid: str, reason: str = "") -> None:
        subject = self._subjects.pop(sid, None)
        for run_id in list(self._rooms):
            self._forget(run_id, sid)

        if subject is not None:
            self._leave_user(subject.user_id, sid)

        logger.info("workflow socket disconnect: sid=%s reason=%s", sid, reason)

    async def on_subscribe(self, sid: str, data: Any) -> None:
        subject = self._subjects.get(sid)
        if subject is None:
            await self._refuse(sid, "not authenticated")
            return

        try:
            wanted = Subscription.model_validate(data)
        except ValidationError as exc:
            await self._refuse(sid, f"bad subscription: {exc}")
            return

        service = await self._service()
        try:
            run = await service.get_run(subject, wanted.run_id)
        except RefusalError as exc:
            await self._refuse(sid, str(exc))
            return

        await self.enter_room(sid, RunRoom.of(run.id))
        self._rooms.setdefault(run.id, set()).add(sid)
        self._ensure_listener(service, run.id)

        snapshot = RunSnapshot(run_id=run.id, status=run.status, state=run.state)
        await self.emit(
            WorkflowSocketEvent.RUN_STATE.value, self._payload(snapshot), to=sid
        )

    async def on_unsubscribe(self, sid: str, data: Any) -> None:
        try:
            wanted = Subscription.model_validate(data)
        except ValidationError as exc:
            await self._refuse(sid, f"bad subscription: {exc}")
            return

        await self.leave_room(sid, RunRoom.of(wanted.run_id))
        self._forget(wanted.run_id, sid)

    def _ensure_listener(self, service: WorkflowService, run_id: UUID) -> None:
        if run_id in self._leaves:
            return

        async def deliver(envelope: Envelope) -> None:
            if envelope.message.kind not in self.RUN_MESSAGES:
                return

            # снимок не прочитан — страница узнаёт об этом событием, а не молчанием
            try:
                snapshot = await service.snapshot_of(run_id)
            except Exception as exc:
                logger.exception("run %s: snapshot is not available", run_id)
                await self.emit(
                    WorkflowSocketEvent.REFUSED.value,
                    {"reason": f"run state is not available: {exc}"},
                    room=RunRoom.of(run_id),
                )
                return

            await self.emit(
                WorkflowSocketEvent.RUN_STATE.value,
                self._payload(snapshot),
                room=RunRoom.of(run_id),
            )

        self._leaves[run_id] = service.bus.subscribe(Scope.workflow(run_id), deliver)

    def _forget(self, run_id: UUID, sid: str) -> None:
        sids = self._rooms.get(run_id)
        if sids is None:
            return

        sids.discard(sid)
        if sids:
            return

        del self._rooms[run_id]
        leave = self._leaves.pop(run_id, None)
        if leave is not None:
            leave()

    async def _refuse(self, sid: str, reason: str) -> None:
        logger.info("workflow socket refused subscription: sid=%s %s", sid, reason)
        await self.emit(WorkflowSocketEvent.REFUSED.value, {"reason": reason}, to=sid)

    @staticmethod
    def _payload(snapshot: RunSnapshot) -> dict[str, Any]:
        return snapshot.model_dump(mode="json")


class WorkflowSocket:
    """Сервер socket.io API и его ASGI-приложение; хост монтирует его под PATH."""

    PATH: ClassVar[str] = StudioPath.SOCKET

    @classmethod
    def build(cls, namespace: WorkflowNamespace) -> socketio.ASGIApp:
        # origin по умолчанию: свой хост, с учётом X-Forwarded-Host/Proto за прокси;
        # пустой список отвергал любой Origin браузера, и живые статусы не доходили
        server = socketio.AsyncServer(async_mode="asgi")
        server.register_namespace(namespace)

        # путь проверяет Mount приложения; сам engine.io путь не сверяет
        return socketio.ASGIApp(socketio_server=server, socketio_path="")
