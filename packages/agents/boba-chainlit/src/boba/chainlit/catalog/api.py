"""JSON API каталога под {prefix}/api/catalog: тонкие маршруты над CatalogService.

Пользователь берётся из cookie входа chainlit; субъект собирается по строке
users и ролям входа под профилем по умолчанию: каталог не выдаёт инструментов,
поэтому видимость профиля ролям не проверяется. Маршрут разбирает запрос в
модель и зовёт сервис; логики здесь нет.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — CatalogRefusalError: нет роли или не владелец.
404 — процесс, черновик, ссылка, версия снимка, объект или синхронизация
    не найдены.
409 — DraftConflictError с {current_seq}, DraftStaleError с {current_version},
    DraftClosedError, ProcessNameTakenError, ConnectionInUseError,
    SnapshotKindMismatchError, SyncRunningError, SyncClosedError.
422 — CatalogOpError с {index, reason}; UnknownSourceKindError, SyncSetupError;
    негодное тело запроса (FastAPI).
503 — CatalogStoreError: хранилище каталога недоступно; сервис не поднят.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ObjectCard,
    ObjectKind,
    ObjectRef,
    OperationList,
    SourceDiff,
    SourceKindsError,
    Staleness,
    TreeNode,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogAccess,
    CatalogRefusalError,
    CatalogService,
    CatalogStoreError,
    ConnectionInUseError,
    ConnectionNotSyncedError,
    ConnectionVersion,
    ConnectionVersionNotFoundError,
    Draft,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftStaleError,
    DraftState,
    ObjectNotFoundError,
    PinBump,
    Process,
    ProcessContext,
    ProcessNameTakenError,
    ProcessNotFoundError,
    ProcessSpec,
    RebaseResult,
    Share,
    SharedNodeNotFoundError,
    SharedProcess,
    ShareNotFoundError,
    SnapshotKindMismatchError,
    Sync,
    SyncCaller,
    SyncClosedError,
    SyncedConnection,
    SyncNotFoundError,
    SyncRunningError,
    SyncScope,
    SyncSetupError,
    UnknownSourceKindError,
    Version,
)
from boba.chainlit.catalog.subjects import ChainlitSubjects, SignedIn
from boba.identity.context import HumanInitiator, Scope, Subject
from boba.messaging import CatalogChanged, Envelope, MessageBus
from chainlit.user import PersistedUser, User

__all__ = [
    "CatalogApi",
    "CatalogEvents",
    "CatalogUrl",
    "DraftBody",
    "DraftNameBody",
    "OpsBody",
    "RebaseBody",
    "SignedIn",
]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[CatalogService]]

T = TypeVar("T")


CurrentUser = Annotated[User | PersistedUser | None, Depends(SignedIn.user)]


class CatalogUrl(StrEnum):
    """Пути ресурсов каталога относительно префикса api."""

    PREFIX = "/api/catalog"
    ACCESS = "/access"
    EVENTS = "/events"
    PROCESSES = "/processes"
    PROCESS = "/processes/{process_id}"
    PROCESS_SNAPSHOT = "/processes/{process_id}/snapshot"
    PROCESS_VERSIONS = "/processes/{process_id}/versions"
    PROCESS_CONTEXT = "/processes/{process_id}/context"
    PROCESS_STALENESS = "/processes/{process_id}/staleness"
    PROCESS_SHARES = "/processes/{process_id}/shares"
    SHARE = "/shares/{token}"
    SHARED = "/shared/{token}"
    SHARED_OBJECT = "/shared/{token}/nodes/{node_id}/object"
    DRAFTS = "/drafts"
    DRAFT = "/drafts/{draft_id}"
    DRAFT_OPS = "/drafts/{draft_id}/ops"
    DRAFT_PUBLISH = "/drafts/{draft_id}/publish"
    DRAFT_REBASE = "/drafts/{draft_id}/rebase"
    DRAFT_STALENESS = "/drafts/{draft_id}/staleness"
    DRAFT_PINS = "/drafts/{draft_id}/pins"
    DRAFT_CONTEXT = "/drafts/{draft_id}/context"
    SOURCE_KINDS = "/source-kinds"
    SYNCED = "/synced"
    CONNECTION_VERSIONS = "/connections/{connection_id}/versions"
    CONNECTION_TREE = "/connections/{connection_id}/tree"
    CONNECTION_OBJECT = "/connections/{connection_id}/object"
    CONNECTION_DIFF = "/connections/{connection_id}/diff"
    CONNECTION_SYNCS = "/connections/{connection_id}/syncs"
    SYNC = "/syncs/{sync_id}"


class DraftBody(BaseModel):
    """Новый черновик над текущей версией процесса; без процесса — черновик
    нового процесса, имя станет именем процесса при публикации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: UUID | None
    name: str = Field(min_length=1)


class DraftNameBody(BaseModel):
    """Новое имя черновика."""

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


class Deleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted: bool


class Forgotten(BaseModel):
    """Сколько версий снимка забыто."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    versions: int = Field(ge=0)


class SnapshotBody(BaseModel):
    """Снимок целиком: путь стенда. Форма снимка зависит от вида подключения,
    поэтому тело разбирает реестр видов сервиса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: dict[str, Any]


class LatestVersion:
    """Отрицательный номер версии в запросах — последняя версия снимка."""

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

    def __init__(self, service: ServiceSource, subjects: ChainlitSubjects) -> None:
        self._service = service
        self._subjects = subjects

    def mount(self, router: APIRouter) -> None:
        routes = (
            (CatalogUrl.ACCESS, self.access, "GET"),
            (CatalogUrl.EVENTS, self.events, "GET"),
            (CatalogUrl.PROCESSES, self.list_processes, "GET"),
            (CatalogUrl.PROCESSES, self.create_process, "POST"),
            (CatalogUrl.PROCESS, self.get_process, "GET"),
            (CatalogUrl.PROCESS, self.update_process, "PUT"),
            (CatalogUrl.PROCESS, self.delete_process, "DELETE"),
            (CatalogUrl.PROCESS_SNAPSHOT, self.snapshot, "GET"),
            (CatalogUrl.PROCESS_VERSIONS, self.versions, "GET"),
            (CatalogUrl.PROCESS_CONTEXT, self.context, "GET"),
            (CatalogUrl.PROCESS_STALENESS, self.staleness, "GET"),
            (CatalogUrl.DRAFTS, self.my_drafts, "GET"),
            (CatalogUrl.DRAFTS, self.create_draft, "POST"),
            (CatalogUrl.PROCESS_SHARES, self.shares, "GET"),
            (CatalogUrl.PROCESS_SHARES, self.share, "POST"),
            (CatalogUrl.SHARE, self.revoke_share, "DELETE"),
            (CatalogUrl.SHARED, self.shared, "GET"),
            (CatalogUrl.SHARED_OBJECT, self.shared_object, "GET"),
            (CatalogUrl.DRAFT, self.draft_state, "GET"),
            (CatalogUrl.DRAFT, self.rename_draft, "PUT"),
            (CatalogUrl.DRAFT, self.discard_draft, "DELETE"),
            (CatalogUrl.DRAFT_OPS, self.append_ops, "POST"),
            (CatalogUrl.DRAFT_PUBLISH, self.publish, "POST"),
            (CatalogUrl.DRAFT_REBASE, self.rebase, "POST"),
            (CatalogUrl.DRAFT_STALENESS, self.draft_staleness, "GET"),
            (CatalogUrl.DRAFT_PINS, self.bump_pins, "POST"),
            (CatalogUrl.DRAFT_CONTEXT, self.draft_context, "GET"),
            (CatalogUrl.SOURCE_KINDS, self.source_kinds, "GET"),
            (CatalogUrl.SYNCED, self.synced_connections, "GET"),
            (CatalogUrl.CONNECTION_VERSIONS, self.connection_versions, "GET"),
            (CatalogUrl.CONNECTION_VERSIONS, self.write_connection_version, "POST"),
            (CatalogUrl.CONNECTION_VERSIONS, self.forget_versions, "DELETE"),
            (CatalogUrl.CONNECTION_TREE, self.connection_tree, "GET"),
            (CatalogUrl.CONNECTION_OBJECT, self.connection_object, "GET"),
            (CatalogUrl.CONNECTION_DIFF, self.connection_diff, "GET"),
            (CatalogUrl.CONNECTION_SYNCS, self.connection_syncs, "GET"),
            (CatalogUrl.CONNECTION_SYNCS, self.start_sync, "POST"),
            (CatalogUrl.SYNC, self.get_sync, "GET"),
            (CatalogUrl.SYNC, self.cancel_sync, "DELETE"),
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

    # --- процессы ---

    async def list_processes(self, current_user: CurrentUser) -> Sequence[Process]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.list_processes(subject))

    async def create_process(
        self, body: ProcessSpec, current_user: CurrentUser
    ) -> Process:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.create_process(subject, body))

    async def get_process(self, process_id: UUID, current_user: CurrentUser) -> Process:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.process(subject, process_id))

    async def update_process(
        self, process_id: UUID, body: ProcessSpec, current_user: CurrentUser
    ) -> Process:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.update_process(subject, process_id, body))

    async def delete_process(
        self, process_id: UUID, current_user: CurrentUser
    ) -> Deleted:
        subject = self._subject(current_user)
        service = await self._resolved()

        deleted = await self._guarded(service.delete_process(subject, process_id))
        return Deleted(deleted=deleted)

    async def snapshot(
        self, process_id: UUID, current_user: CurrentUser
    ) -> CatalogSnapshot:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.snapshot(subject, process_id))

    async def versions(
        self, process_id: UUID, current_user: CurrentUser
    ) -> Sequence[Version]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.versions(subject, process_id))

    async def context(
        self, process_id: UUID, current_user: CurrentUser
    ) -> ProcessContext:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.context(subject, process_id))

    async def staleness(self, process_id: UUID, current_user: CurrentUser) -> Staleness:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.staleness(subject, process_id))

    # --- черновики ---

    async def my_drafts(self, current_user: CurrentUser) -> Sequence[Draft]:
        """Открытые черновики пользователя по всем процессам."""
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.my_drafts(subject))

    async def create_draft(self, body: DraftBody, current_user: CurrentUser) -> Draft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.create_draft(subject, body.process_id, body.name)
        )

    async def rename_draft(
        self, draft_id: UUID, body: DraftNameBody, current_user: CurrentUser
    ) -> Draft:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.rename_draft(subject, draft_id, body.name))

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

    async def draft_staleness(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> Staleness:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.draft_staleness(subject, draft_id))

    async def draft_context(
        self, draft_id: UUID, current_user: CurrentUser
    ) -> ProcessContext:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.draft_context(subject, draft_id))

    async def bump_pins(self, draft_id: UUID, current_user: CurrentUser) -> PinBump:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.bump_pins(subject, draft_id))

    # --- ссылки на просмотр ---

    async def shares(
        self, process_id: UUID, current_user: CurrentUser
    ) -> Sequence[Share]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.shares(subject, process_id))

    async def share(self, process_id: UUID, current_user: CurrentUser) -> Share:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.share_process(subject, process_id))

    async def revoke_share(self, token: str, current_user: CurrentUser) -> Share:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.revoke_share(subject, token))

    async def shared(self, token: str) -> SharedProcess:
        """Опубликованный процесс по ссылке: без входа и прав на каталог."""
        service = await self._resolved()

        return await self._guarded(service.shared_process(token))

    async def shared_object(
        self, token: str, node_id: UUID
    ) -> SerializeAsAny[ObjectCard]:
        service = await self._resolved()

        return await self._guarded(service.shared_object(token, node_id))

    # --- подключения глазами каталога ---

    async def source_kinds(self, current_user: CurrentUser) -> Sequence[str]:
        """Виды подключений с установленным снимком: kind типов соединений."""
        self._subject(current_user)
        service = await self._resolved()

        return service.source_kinds()

    async def synced_connections(
        self, current_user: CurrentUser
    ) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка: имя и вид на момент последнего снятия."""
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.synced_connections(subject))

    async def connection_versions(
        self, connection_id: UUID, current_user: CurrentUser
    ) -> Sequence[ConnectionVersion]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.connection_versions(subject, connection_id))

    async def write_connection_version(
        self, connection_id: UUID, body: SnapshotBody, current_user: CurrentUser
    ) -> ConnectionVersion:
        subject = self._subject(current_user)
        service = await self._resolved()

        try:
            snapshot = service.connections.kinds.parse(body.snapshot)
        except SourceKindsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return await self._guarded(
            service.write_connection_version(subject, connection_id, snapshot)
        )

    async def forget_versions(
        self, connection_id: UUID, current_user: CurrentUser
    ) -> Forgotten:
        """Все версии снимка подключения; отказ, пока оно стоит в узлах."""
        subject = self._subject(current_user)
        service = await self._resolved()

        forgotten = await self._guarded(service.forget_versions(subject, connection_id))
        return Forgotten(versions=forgotten)

    async def connection_tree(
        self,
        connection_id: UUID,
        current_user: CurrentUser,
        version: int = LatestVersion.QUERY,
        path: Annotated[list[str], Query()] = [],  # noqa: B006
    ) -> Sequence[TreeNode]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.connection_tree(subject, connection_id, version, path)
        )

    async def connection_object(
        self,
        connection_id: UUID,
        kind: ObjectKind,
        path: Annotated[list[str], Query()],
        current_user: CurrentUser,
        version: int = LatestVersion.QUERY,
    ) -> SerializeAsAny[ObjectCard]:
        subject = self._subject(current_user)
        service = await self._resolved()

        ref = ObjectRef(connection_id=connection_id, kind=kind, path=tuple(path))
        return await self._guarded(service.connection_object(subject, ref, version))

    async def connection_diff(
        self, connection_id: UUID, old: int, new: int, current_user: CurrentUser
    ) -> SourceDiff:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(
            service.connection_diff(subject, connection_id, old, new)
        )

    # --- синхронизации ---

    async def connection_syncs(
        self, connection_id: UUID, current_user: CurrentUser
    ) -> Sequence[Sync]:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.connection_syncs(subject, connection_id))

    async def start_sync(
        self, connection_id: UUID, body: SyncScope, current_user: CurrentUser
    ) -> Sync:
        """Синхронизация подключения инструментом вида от имени пользователя
        входа: возвращает запись сразу, ход виден по GET и событиям."""
        caller = self._caller(current_user)
        service = await self._resolved()

        return await self._guarded(service.start_sync(caller, connection_id, body))

    async def get_sync(self, sync_id: UUID, current_user: CurrentUser) -> Sync:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.sync(subject, sync_id))

    async def cancel_sync(self, sync_id: UUID, current_user: CurrentUser) -> Sync:
        subject = self._subject(current_user)
        service = await self._resolved()

        return await self._guarded(service.cancel_sync(subject, sync_id))

    def _subject(self, current_user: User | PersistedUser | None) -> Subject:
        """Субъект по строке users под профилем по умолчанию для ролей входа."""
        return self._subjects.of_user(current_user).subject

    def _caller(self, current_user: User | PersistedUser | None) -> SyncCaller:
        """Субъект входа с его секретами: инструмент снятия ходит в базу
        под билетом пользователя."""
        identity = self._subjects.of_user(current_user)

        return SyncCaller(
            subject=identity.subject,
            initiator=HumanInitiator(via="api"),
            credential=identity.credential,
        )

    async def _resolved(self) -> CatalogService:
        try:
            return await self._service()
        except RuntimeError as exc:
            msg = f"catalog service is not available: {exc}"
            raise HTTPException(status_code=503, detail=msg) from exc

    @staticmethod
    async def _guarded(action: Awaitable[T]) -> T:
        """Отказы сервиса и хранилища — в HTTP-статусы."""
        try:
            return await action
        except CatalogRefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (
            ProcessNotFoundError,
            DraftNotFoundError,
            ShareNotFoundError,
            SharedNodeNotFoundError,
            ConnectionNotSyncedError,
            ConnectionVersionNotFoundError,
            ObjectNotFoundError,
            SyncNotFoundError,
        ) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ProcessNameTakenError,
            ConnectionInUseError,
            SnapshotKindMismatchError,
            SyncRunningError,
            SyncClosedError,
            DraftClosedError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (UnknownSourceKindError, SyncSetupError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DraftConflictError as exc:
            detail: dict[str, Any] = {
                "message": str(exc),
                "current_seq": exc.current_seq,
            }
            raise HTTPException(status_code=409, detail=detail) from exc
        except DraftStaleError as exc:
            detail = {"message": str(exc), "current_version": exc.current_version}
            raise HTTPException(status_code=409, detail=detail) from exc
        except CatalogOpError as exc:
            detail = {"message": str(exc), "index": exc.index, "reason": exc.reason}
            raise HTTPException(status_code=422, detail=detail) from exc
        except CatalogStoreError as exc:
            logger.error("catalog api: store failure: %s", exc)
            msg = f"catalog store failure: {exc}"
            raise HTTPException(status_code=503, detail=msg) from exc
