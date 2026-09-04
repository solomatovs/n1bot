"""JSON API каталога под {prefix}/api/catalog: тонкие маршруты над CatalogService.

Пользователь берётся из cookie входа chainlit; субъект собирается по строке
users и ролям входа под профилем по умолчанию: каталог не выдаёт инструментов,
поэтому видимость профиля ролям не проверяется. Маршрут разбирает запрос в
модель и зовёт сервис; логики здесь нет.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — CatalogRefusalError: нет роли или шаринга.
404 — черновик или вид не найден.
409 — DraftConflictError с {current_seq}, DraftStaleError с {current_version},
    DraftClosedError.
422 — CatalogOpError с {index, reason}; негодное тело запроса (FastAPI).
503 — CatalogStoreError: хранилище каталога недоступно; сервис не поднят.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from boba.catalog import CatalogOpError, CatalogSnapshot, OperationList
from boba.catalog_service import (
    AuthorVia,
    CatalogRefusalError,
    CatalogService,
    CatalogStoreError,
    Draft,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftStaleError,
    DraftState,
    NodePosition,
    RebaseResult,
    ShareTargetKind,
    Version,
    View,
    ViewLayout,
    ViewNotFoundError,
    ViewShare,
    ViewSpec,
)
from boba.chainlit.infra.session import ChainlitSession
from boba.chat.profiles import ChatProfiles
from boba.identity.context import Scope, Subject
from boba.messaging import CatalogChanged, Envelope, MessageBus
from chainlit.auth import get_current_user, reuseable_oauth
from chainlit.user import PersistedUser, User

__all__ = [
    "CatalogApi",
    "CatalogEvents",
    "CatalogUrl",
    "DraftBody",
    "LayoutBody",
    "OpsBody",
    "RebaseBody",
    "SignedIn",
]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[CatalogService]]

T = TypeVar("T")


class SignedIn:
    """Пользователь входа chainlit по запросу: токен из cookie или Authorization.

    Обёртка вместо прямого Depends(get_current_user): схема безопасности
    chainlit ломает генерацию OpenAPI, а тесты подменяют одну зависимость.
    """

    @staticmethod
    async def user(request: Request) -> User | PersistedUser | None:
        token = await reuseable_oauth(request)
        if token is None:
            return None

        return await get_current_user(token)


CurrentUser = Annotated[User | PersistedUser | None, Depends(SignedIn.user)]


class CatalogUrl(StrEnum):
    """Пути ресурсов каталога относительно префикса api."""

    PREFIX = "/api/catalog"
    SNAPSHOT = "/snapshot"
    VERSIONS = "/versions"
    DRAFTS = "/drafts"
    DRAFT = "/drafts/{draft_id}"
    DRAFT_OPS = "/drafts/{draft_id}/ops"
    DRAFT_PUBLISH = "/drafts/{draft_id}/publish"
    DRAFT_REBASE = "/drafts/{draft_id}/rebase"
    VIEWS = "/views"
    VIEW = "/views/{view_id}"
    VIEW_LAYOUT = "/views/{view_id}/layout"
    VIEW_SHARES = "/views/{view_id}/shares"
    VIEW_SHARE = "/views/{view_id}/shares/{kind}/{target}"
    EVENTS = "/events"


class DraftBody(BaseModel):
    """Новый черновик над текущей версией."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


class OpsBody(BaseModel):
    """Порция операций с номером, на который она рассчитана."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_seq: int = Field(ge=0)
    operations: OperationList


class RebaseBody(BaseModel):
    """Перебазирование: с drop_conflicts конфликтные операции вычёркиваются."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drop_conflicts: bool


class LayoutBody(BaseModel):
    """Полная раскладка вида."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    positions: tuple[NodePosition, ...]


class Deleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted: bool


class CatalogEvents:
    """Поток server-sent events с CatalogChanged области пользователя.

    Страница каталога живёт вне чата, сокет chainlit ей недоступен, поэтому
    правки черновиков и видов доходят до неё этим потоком: одна строка data на
    сообщение, комментарий-пульс, пока тихо. Подписка снимается с обрывом
    соединения.
    """

    MEDIA_TYPE: ClassVar[str] = "text/event-stream"
    HEARTBEAT_SEC: ClassVar[float] = 15.0
    HEARTBEAT: ClassVar[str] = ": ping\n\n"

    def __init__(self, bus: MessageBus, user_id: UUID) -> None:
        self._bus = bus
        self._scope = Scope.user(user_id)

    def response(self) -> StreamingResponse:
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(
            self.frames(), media_type=self.MEDIA_TYPE, headers=headers
        )

    async def frames(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[CatalogChanged] = asyncio.Queue()

        async def deliver(envelope: Envelope) -> None:
            if isinstance(envelope.message, CatalogChanged):
                await queue.put(envelope.message)

        leave = self._bus.subscribe(self._scope, deliver)
        try:
            yield self.HEARTBEAT
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), self.HEARTBEAT_SEC)
                except TimeoutError:
                    yield self.HEARTBEAT
                    continue

                yield f"data: {message.model_dump_json()}\n\n"
        finally:
            leave()


class CatalogApi:
    """Обработчики JSON API каталога; сервис берётся на каждый запрос."""

    TAG: ClassVar[str] = "catalog"

    def __init__(self, service: ServiceSource, profiles: ChatProfiles) -> None:
        self._service = service
        self._profiles = profiles

    def mount(self, router: APIRouter) -> None:
        routes = (
            (CatalogUrl.SNAPSHOT, self.snapshot, "GET"),
            (CatalogUrl.VERSIONS, self.versions, "GET"),
            (CatalogUrl.DRAFTS, self.list_drafts, "GET"),
            (CatalogUrl.DRAFTS, self.create_draft, "POST"),
            (CatalogUrl.DRAFT, self.draft_state, "GET"),
            (CatalogUrl.DRAFT, self.discard_draft, "DELETE"),
            (CatalogUrl.DRAFT_OPS, self.append_ops, "POST"),
            (CatalogUrl.DRAFT_PUBLISH, self.publish, "POST"),
            (CatalogUrl.DRAFT_REBASE, self.rebase, "POST"),
            (CatalogUrl.VIEWS, self.list_views, "GET"),
            (CatalogUrl.VIEWS, self.create_view, "POST"),
            (CatalogUrl.VIEW, self.get_view, "GET"),
            (CatalogUrl.VIEW, self.update_view, "PUT"),
            (CatalogUrl.VIEW, self.delete_view, "DELETE"),
            (CatalogUrl.VIEW_LAYOUT, self.layout, "GET"),
            (CatalogUrl.VIEW_LAYOUT, self.put_layout, "PUT"),
            (CatalogUrl.VIEW_SHARES, self.shares, "GET"),
            (CatalogUrl.VIEW_SHARES, self.share, "POST"),
            (CatalogUrl.VIEW_SHARE, self.unshare, "DELETE"),
            (CatalogUrl.EVENTS, self.events, "GET"),
        )
        for path, handler, method in routes:
            router.add_api_route(path.value, handler, methods=[method], tags=[self.TAG])

    async def events(self, current_user: CurrentUser) -> StreamingResponse:
        """Поток CatalogChanged пользователя; право — как на чтение каталога."""
        subject = self._subject(current_user)
        service = await self._resolved()
        if not service.can_view(subject):
            msg = f"user {subject.login!r} has no role to read the catalog"
            raise HTTPException(status_code=403, detail=msg)

        return CatalogEvents(service.bus, subject.user_id).response()

    async def snapshot(self, current_user: CurrentUser) -> CatalogSnapshot:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.snapshot(subject))

    async def versions(self, current_user: CurrentUser) -> Sequence[Version]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.versions(subject))

    async def list_drafts(self, current_user: CurrentUser) -> Sequence[Draft]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.open_drafts(subject))

    async def create_draft(self, body: DraftBody, current_user: CurrentUser) -> Draft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.create_draft(subject, body.name))

    async def draft_state(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> DraftState:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.draft_state(subject, draft_id))

    async def discard_draft(self, draft_id: UUID, current_user: CurrentUser) -> Draft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.discard_draft(subject, draft_id))

    async def append_ops(
        self, draft_id: UUID, body: OpsBody, current_user: CurrentUser
    ) -> DraftState:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.append_ops(
                subject, draft_id, body.expected_seq, body.operations, AuthorVia.USER
            )
        )

    async def publish(self, draft_id: UUID, current_user: CurrentUser) -> Version:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.publish(subject, draft_id, AuthorVia.USER))

    async def rebase(
        self, draft_id: UUID, body: RebaseBody, current_user: CurrentUser
    ) -> RebaseResult:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.rebase(subject, draft_id, drop_conflicts=body.drop_conflicts)
        )

    async def list_views(self, current_user: CurrentUser) -> Sequence[View]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.views(subject))

    async def create_view(self, body: ViewSpec, current_user: CurrentUser) -> View:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.create_view(subject, body))

    async def get_view(self, view_id: UUID, current_user: CurrentUser) -> View:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.view(subject, view_id))

    async def update_view(
        self, view_id: UUID, body: ViewSpec, current_user: CurrentUser
    ) -> View:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.update_view(subject, view_id, body))

    async def delete_view(self, view_id: UUID, current_user: CurrentUser) -> Deleted:
        subject = self._subject(current_user)
        service = await self._resolved()

        deleted = await self._guarded(service.delete_view(subject, view_id))
        return Deleted(deleted=deleted)

    async def layout(self, view_id: UUID, current_user: CurrentUser) -> ViewLayout:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.layout(subject, view_id))

    async def put_layout(
        self, view_id: UUID, body: LayoutBody, current_user: CurrentUser
    ) -> ViewLayout:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.put_layout(subject, view_id, body.positions))

    async def shares(
        self, view_id: UUID, current_user: CurrentUser
    ) -> Sequence[ViewShare]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.shares(subject, view_id))

    async def share(
        self, view_id: UUID, body: ViewShare, current_user: CurrentUser
    ) -> Response:
        subject = self._subject(current_user)
        service = await self._resolved()

        await self._guarded(service.share_view(subject, view_id, body))
        return Response(status_code=204)

    async def unshare(
        self,
        view_id: UUID,
        kind: ShareTargetKind,
        target: str,
        current_user: CurrentUser,
    ) -> Deleted:
        subject = self._subject(current_user)
        service = await self._resolved()

        share = ViewShare(kind=kind, target=target)
        removed = await self._guarded(service.unshare_view(subject, view_id, share))
        return Deleted(deleted=removed)

    def _subject(self, current_user: User | PersistedUser | None) -> Subject:
        """Субъект по строке users под профилем по умолчанию для ролей входа."""
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            user_id = UUID(current_user.id)
        except ValueError as exc:
            msg = f"user id {current_user.id!r} is not the users.id uuid"
            raise HTTPException(status_code=401, detail=msg) from exc

        roles = ChainlitSession.roles_of(current_user)
        profile = self._profiles.default_name()

        return Subject.of_user(user_id, current_user.identifier, roles, profile)

    async def _resolved(self) -> CatalogService:
        try:
            return await self._service()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @staticmethod
    async def _guarded(action: Awaitable[T]) -> T:
        """Отказы сервиса и хранилища — в HTTP-статусы."""
        try:
            return await action
        except CatalogRefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (DraftNotFoundError, ViewNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DraftConflictError as exc:
            detail: dict[str, Any] = {
                "message": str(exc),
                "current_seq": exc.current_seq,
            }
            raise HTTPException(status_code=409, detail=detail) from exc
        except DraftStaleError as exc:
            detail = {"message": str(exc), "current_version": exc.current_version}
            raise HTTPException(status_code=409, detail=detail) from exc
        except DraftClosedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CatalogOpError as exc:
            detail = {"message": str(exc), "index": exc.index, "reason": exc.reason}
            raise HTTPException(status_code=422, detail=detail) from exc
        except CatalogStoreError as exc:
            logger.error("catalog api: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
