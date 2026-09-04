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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ObjectCard,
    ObjectKind,
    ObjectRef,
    OperationList,
    SourceDiff,
    SourceOperationList,
    SourceOpError,
    SourceSnapshot,
    Staleness,
    TreeNode,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogAccess,
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
    PinBump,
    RebaseResult,
    ShareTargetKind,
    Source,
    SourceConnection,
    SourceDraft,
    SourceDraftNotFoundError,
    SourceDraftState,
    SourceNotFoundError,
    SourceNotManualError,
    SourceObjectNotFoundError,
    SourceSpec,
    SourceVersion,
    SourceVersionNotFoundError,
    Version,
    View,
    ViewLayout,
    ViewNotFoundError,
    ViewShare,
    ViewSpec,
    ViewState,
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
    ACCESS = "/access"
    SNAPSHOT = "/snapshot"
    VERSIONS = "/versions"
    DRAFTS = "/drafts"
    DRAFT = "/drafts/{draft_id}"
    DRAFT_OPS = "/drafts/{draft_id}/ops"
    DRAFT_PUBLISH = "/drafts/{draft_id}/publish"
    DRAFT_REBASE = "/drafts/{draft_id}/rebase"
    DRAFT_STALENESS = "/drafts/{draft_id}/staleness"
    DRAFT_PINS = "/drafts/{draft_id}/pins"
    STALENESS = "/staleness"
    VIEWS = "/views"
    VIEW = "/views/{view_id}"
    VIEW_STATE = "/views/{view_id}/state"
    VIEW_LAYOUT = "/views/{view_id}/layout"
    VIEW_SHARES = "/views/{view_id}/shares"
    VIEW_SHARE = "/views/{view_id}/shares/{kind}/{target}"
    EVENTS = "/events"
    SOURCES = "/sources"
    SOURCE = "/sources/{source_id}"
    SOURCE_CONNECTIONS = "/sources/{source_id}/connections"
    SOURCE_CONNECTION = "/sources/{source_id}/connections/{connection_id}"
    SOURCE_VERSIONS = "/sources/{source_id}/versions"
    SOURCE_TREE = "/sources/{source_id}/tree"
    SOURCE_OBJECT = "/sources/{source_id}/object"
    SOURCE_DIFF = "/sources/{source_id}/diff"
    SOURCE_DRAFTS = "/sources/{source_id}/drafts"
    SOURCE_DRAFT = "/source-drafts/{draft_id}"
    SOURCE_DRAFT_OPS = "/source-drafts/{draft_id}/ops"
    SOURCE_DRAFT_TREE = "/source-drafts/{draft_id}/tree"
    SOURCE_DRAFT_OBJECT = "/source-drafts/{draft_id}/object"
    SOURCE_DRAFT_PUBLISH = "/source-drafts/{draft_id}/publish"


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


class ConnectionBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID


class SourceOpsBody(BaseModel):
    """Порция операций ручного источника с номером, на который она рассчитана."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_seq: int = Field(ge=0)
    operations: SourceOperationList


class SnapshotBody(BaseModel):
    """Снимок целиком: путь стенда и переноса из staging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: SourceSnapshot = Field(discriminator="kind")


class LatestVersion:
    """Отрицательный номер версии в запросах — последняя версия источника."""

    QUERY = -1


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
            (CatalogUrl.ACCESS, self.access, "GET"),
            (CatalogUrl.SNAPSHOT, self.snapshot, "GET"),
            (CatalogUrl.VERSIONS, self.versions, "GET"),
            (CatalogUrl.DRAFTS, self.list_drafts, "GET"),
            (CatalogUrl.DRAFTS, self.create_draft, "POST"),
            (CatalogUrl.DRAFT, self.draft_state, "GET"),
            (CatalogUrl.DRAFT, self.discard_draft, "DELETE"),
            (CatalogUrl.DRAFT_OPS, self.append_ops, "POST"),
            (CatalogUrl.DRAFT_PUBLISH, self.publish, "POST"),
            (CatalogUrl.DRAFT_REBASE, self.rebase, "POST"),
            (CatalogUrl.DRAFT_STALENESS, self.draft_staleness, "GET"),
            (CatalogUrl.DRAFT_PINS, self.bump_pins, "POST"),
            (CatalogUrl.STALENESS, self.staleness, "GET"),
            (CatalogUrl.VIEWS, self.list_views, "GET"),
            (CatalogUrl.VIEWS, self.create_view, "POST"),
            (CatalogUrl.VIEW, self.get_view, "GET"),
            (CatalogUrl.VIEW, self.update_view, "PUT"),
            (CatalogUrl.VIEW, self.delete_view, "DELETE"),
            (CatalogUrl.VIEW_STATE, self.view_state, "GET"),
            (CatalogUrl.VIEW_LAYOUT, self.layout, "GET"),
            (CatalogUrl.VIEW_LAYOUT, self.put_layout, "PUT"),
            (CatalogUrl.VIEW_SHARES, self.shares, "GET"),
            (CatalogUrl.VIEW_SHARES, self.share, "POST"),
            (CatalogUrl.VIEW_SHARE, self.unshare, "DELETE"),
            (CatalogUrl.EVENTS, self.events, "GET"),
            (CatalogUrl.SOURCES, self.list_sources, "GET"),
            (CatalogUrl.SOURCES, self.create_source, "POST"),
            (CatalogUrl.SOURCE, self.get_source, "GET"),
            (CatalogUrl.SOURCE, self.update_source, "PUT"),
            (CatalogUrl.SOURCE, self.delete_source, "DELETE"),
            (CatalogUrl.SOURCE_CONNECTIONS, self.source_connections, "GET"),
            (CatalogUrl.SOURCE_CONNECTIONS, self.bind_connection, "POST"),
            (CatalogUrl.SOURCE_CONNECTION, self.unbind_connection, "DELETE"),
            (CatalogUrl.SOURCE_VERSIONS, self.source_versions, "GET"),
            (CatalogUrl.SOURCE_VERSIONS, self.write_source_version, "POST"),
            (CatalogUrl.SOURCE_TREE, self.source_tree, "GET"),
            (CatalogUrl.SOURCE_OBJECT, self.source_object, "GET"),
            (CatalogUrl.SOURCE_DIFF, self.source_diff, "GET"),
            (CatalogUrl.SOURCE_DRAFTS, self.source_drafts, "GET"),
            (CatalogUrl.SOURCE_DRAFTS, self.create_source_draft, "POST"),
            (CatalogUrl.SOURCE_DRAFT, self.source_draft_state, "GET"),
            (CatalogUrl.SOURCE_DRAFT, self.discard_source_draft, "DELETE"),
            (CatalogUrl.SOURCE_DRAFT_OPS, self.append_source_ops, "POST"),
            (CatalogUrl.SOURCE_DRAFT_TREE, self.source_draft_tree, "GET"),
            (CatalogUrl.SOURCE_DRAFT_OBJECT, self.source_draft_object, "GET"),
            (CatalogUrl.SOURCE_DRAFT_PUBLISH, self.publish_source_draft, "POST"),
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

    async def access(self, current_user: CurrentUser) -> CatalogAccess:
        subject = self._subject(current_user)
        service = await self._resolved()

        return service.access(subject)

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

    async def staleness(self, current_user: CurrentUser) -> Staleness:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.staleness(subject))

    async def draft_staleness(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> Staleness:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.draft_staleness(subject, draft_id))

    async def bump_pins(self, draft_id: UUID, current_user: CurrentUser) -> PinBump:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.bump_pins(subject, draft_id))

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

    async def view_state(self, view_id: UUID, current_user: CurrentUser) -> ViewState:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.view_state(subject, view_id))

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

    # --- источники ---

    async def list_sources(self, current_user: CurrentUser) -> Sequence[Source]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.list_sources(subject))

    async def create_source(
        self, body: SourceSpec, current_user: CurrentUser
    ) -> Source:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.create_source(subject, body))

    async def get_source(self, source_id: UUID, current_user: CurrentUser) -> Source:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source(subject, source_id))

    async def update_source(
        self, source_id: UUID, body: SourceSpec, current_user: CurrentUser
    ) -> Source:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.update_source(subject, source_id, body))

    async def delete_source(
        self, source_id: UUID, current_user: CurrentUser
    ) -> Deleted:
        subject = self._subject(current_user)
        service = await self._resolved()

        deleted = await self._guarded(service.delete_source(subject, source_id))
        return Deleted(deleted=deleted)

    async def source_connections(
        self, source_id: UUID, current_user: CurrentUser
    ) -> Sequence[SourceConnection]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_connections(subject, source_id))

    async def bind_connection(
        self, source_id: UUID, body: ConnectionBody, current_user: CurrentUser
    ) -> SourceConnection:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.bind_connection(subject, source_id, body.connection_id)
        )

    async def unbind_connection(
        self, source_id: UUID, connection_id: UUID, current_user: CurrentUser
    ) -> Deleted:
        subject = self._subject(current_user)
        service = await self._resolved()

        removed = await self._guarded(
            service.unbind_connection(subject, source_id, connection_id)
        )
        return Deleted(deleted=removed)

    async def source_versions(
        self, source_id: UUID, current_user: CurrentUser
    ) -> Sequence[SourceVersion]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_versions(subject, source_id))

    async def write_source_version(
        self, source_id: UUID, body: SnapshotBody, current_user: CurrentUser
    ) -> SourceVersion:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.write_source_version(subject, source_id, body.snapshot)
        )

    async def source_tree(
        self,
        source_id: UUID,
        current_user: CurrentUser,
        version: int = LatestVersion.QUERY,
        path: Annotated[list[str], Query()] = [],  # noqa: B006
    ) -> Sequence[TreeNode]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.source_tree(subject, source_id, version, path)
        )

    async def source_object(
        self,
        source_id: UUID,
        kind: ObjectKind,
        path: Annotated[list[str], Query()],
        current_user: CurrentUser,
        version: int = LatestVersion.QUERY,
    ) -> ObjectCard:
        subject = self._subject(current_user)
        service = await self._resolved()

        ref = ObjectRef(source_id=source_id, kind=kind, path=tuple(path))
        return await self._guarded(service.source_object(subject, ref, version))

    async def source_diff(
        self, source_id: UUID, old: int, new: int, current_user: CurrentUser
    ) -> SourceDiff:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_diff(subject, source_id, old, new))

    async def source_drafts(
        self, source_id: UUID, current_user: CurrentUser
    ) -> Sequence[SourceDraft]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_drafts(subject, source_id))

    async def create_source_draft(
        self, source_id: UUID, body: DraftBody, current_user: CurrentUser
    ) -> SourceDraft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.create_source_draft(subject, source_id, body.name)
        )

    async def source_draft_state(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> SourceDraftState:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_draft_state(subject, draft_id))

    async def discard_source_draft(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> SourceDraft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.discard_source_draft(subject, draft_id))

    async def source_draft_tree(
        self,
        draft_id: UUID,
        current_user: CurrentUser,
        path: Annotated[list[str], Query()] = [],  # noqa: B006
    ) -> Sequence[TreeNode]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.source_draft_tree(subject, draft_id, path))

    async def source_draft_object(
        self,
        draft_id: UUID,
        kind: ObjectKind,
        path: Annotated[list[str], Query()],
        current_user: CurrentUser,
    ) -> ObjectCard:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.source_draft_object(subject, draft_id, kind, path)
        )

    async def append_source_ops(
        self, draft_id: UUID, body: SourceOpsBody, current_user: CurrentUser
    ) -> SourceDraftState:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.append_source_ops(
                subject, draft_id, body.expected_seq, body.operations, AuthorVia.USER
            )
        )

    async def publish_source_draft(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> SourceVersion:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.publish_source_draft(subject, draft_id, AuthorVia.USER)
        )

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
        except (
            DraftNotFoundError,
            ViewNotFoundError,
            SourceNotFoundError,
            SourceVersionNotFoundError,
            SourceObjectNotFoundError,
            SourceDraftNotFoundError,
        ) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SourceNotManualError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        except (CatalogOpError, SourceOpError) as exc:
            detail = {"message": str(exc), "index": exc.index, "reason": exc.reason}
            raise HTTPException(status_code=422, detail=detail) from exc
        except CatalogStoreError as exc:
            logger.error("catalog api: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
