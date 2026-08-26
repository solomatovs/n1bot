"""Namespace /workflow на socket.io chainlit: живые снимки запусков странице.

Подключение авторизуется cookie входа; subscribe пускает в комнату запуска
после проверки владения и сразу шлёт текущий снимок, дальше снимки идут по
мере хода запуска. REST остаётся источником истины, сокет — доставка.

Ошибки: своих не выпускает; отказ подключения — ConnectionRefusedError
socket.io, отказ подписки — событие `refused` с причиной.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import socketio
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.chainlit.infra.tool_api import ApiIdentity
from boba.chat.profiles import ChatProfiles
from boba.identity.api import AuthenticatedUser
from boba.identity.context import Subject
from boba.identity.errors import RefusalError
from boba.workflow.events import RunSnapshot
from boba.workflow_engine.service import WorkflowService

__all__ = ["SocketAuthenticator", "WorkflowNamespace", "WorkflowSocketEvent"]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[WorkflowService]]

SocketAuthenticator = Callable[[dict[str, Any]], Awaitable[AuthenticatedUser | None]]
"""WSGI environ подключения -> пользователь входа; None — cookie негодна."""


class WorkflowSocketEvent(StrEnum):
    """События namespace /workflow."""

    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    RUN_STATE = "run_state"
    REFUSED = "refused"


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


class WorkflowNamespace(socketio.AsyncNamespace):
    """Класс-namespace socket.io: подписки страницы на запуски."""

    NAME: ClassVar[str] = "/workflow"

    def __init__(
        self,
        service: ServiceSource,
        profiles: ChatProfiles,
        authenticate: SocketAuthenticator,
    ) -> None:
        super().__init__(self.NAME)
        self._service = service
        self._profiles = profiles
        self._authenticate = authenticate
        self._subjects: dict[str, Subject] = {}
        self._leaves: dict[UUID, Callable[[], None]] = {}
        self._rooms: dict[UUID, set[str]] = {}

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

    async def on_disconnect(self, sid: str, reason: str = "") -> None:
        self._subjects.pop(sid, None)
        for run_id in list(self._rooms):
            self._forget(run_id, sid)

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

        async def deliver(snapshot: RunSnapshot) -> None:
            await self.emit(
                WorkflowSocketEvent.RUN_STATE.value,
                self._payload(snapshot),
                room=RunRoom.of(run_id),
            )

        self._leaves[run_id] = service.events.listen(run_id, deliver)

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
