"""JSON API каталога под {prefix}/api/v1/catalog: тонкие маршруты над
CatalogService.

Субъект приходит из входа studio (CurrentSubject: пользователь, роли, профиль
по умолчанию, билет входа как секреты). Маршрут разбирает запрос в модель и
зовёт сервис; логики здесь нет.

Ошибки (HTTP):
401 — входа нет (ApiAuth).
403 — CatalogRefusalError: нет роли или не владелец.
404 — процесс, черновик, ссылка, версия снимка, объект или синхронизация
    не найдены.
409 — DraftConflictError с {current_seq}, DraftStaleError с {current_version},
    DraftClosedError, ProcessNameTakenError, ConnectionInUseError,
    SnapshotKindMismatchError, SyncRunningError, SyncClosedError.
422 — CatalogOpError с {index, reason}; UnknownSourceKindError,
    SnapshotRejectedError, SyncSetupError;
    негодное тело запроса (FastAPI).
503 — CatalogStoreError: хранилище каталога недоступно; [catalog] выключен —
    ServiceDisabledError, её переводит DomainErrorMiddleware.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ObjectCard,
    ObjectKind,
    ObjectRef,
    OperationList,
    SourceDiff,
    Staleness,
    TreeNode,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogAccess,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogService,
    CatalogServiceError,
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
    SnapshotRejectedError,
    Sync,
    SyncCaller,
    SyncClosedError,
    SyncedConnection,
    SyncNotFoundError,
    SyncRunningError,
    SyncScope,
    SyncSetupError,
    UnknownSourceKindError,
    Upgrade,
    UpgradeClosedError,
    UpgradeNotFoundError,
    UpgradeReport,
    UpgradeRun,
    UpgradeTarget,
    Version,
)
from boba.identity.context import Subject
from boba.studio.api.app import ApiMount
from boba.studio.api.auth import CurrentSubject
from boba.studio.api.workflows import Deleted

__all__ = [
    "CatalogApi",
    "CatalogHttp",
    "CatalogPageUrl",
    "CatalogUrl",
    "CurrentCatalog",
    "DraftBody",
]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[CatalogService]]


class CatalogPageUrl(StrEnum):
    """Адреса страницы каталога относительно префикса приложения."""

    DRAFT = "/catalog/drafts/{draft_id}"
    PROCESS = "/catalog/processes/{process_id}"

    @classmethod
    def draft(cls, prefix: str, draft_id: UUID) -> str:
        return prefix + cls.DRAFT.value.format(draft_id=draft_id)

    @classmethod
    def process(cls, prefix: str, process_id: UUID) -> str:
        return prefix + cls.PROCESS.value.format(process_id=process_id)


class CatalogUrl(StrEnum):
    """Пути ресурсов каталога относительно версии api."""

    PREFIX = "/catalog"
    ACCESS = "/access"
    PROCESSES = "/processes"
    PROCESSES_UPGRADE = "/processes/upgrade"
    PROCESS = "/processes/{process_id}"
    PROCESS_UPGRADE = "/processes/{process_id}/upgrade"
    UPGRADES = "/upgrades"
    UPGRADE = "/upgrades/{run_id}"
    UPGRADE_REPORT = "/upgrades/{run_id}/report"
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
    DRAFT_UPGRADE = "/drafts/{draft_id}/upgrade"
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


class CatalogApi(ApiMount):
    """Обработчики JSON API каталога: маршруты зовут сервис, который
    зависимость CurrentCatalog берёт из источника на каждый запрос; отказы
    сервиса переводит в статусы CatalogHttp."""

    TAG: ClassVar[str] = "catalog"
    STATE_KEY: ClassVar[str] = "catalog_api"

    def __init__(self, service: ServiceSource) -> None:
        self._service = service

    @staticmethod
    async def current(request: Request) -> CatalogService:
        """Зависимость FastAPI: сервис каталога текущего запроса."""
        api = getattr(request.app.state, CatalogApi.STATE_KEY, None)
        if not isinstance(api, CatalogApi):
            got = type(api).__name__
            msg = f"app.state.{CatalogApi.STATE_KEY}: expected CatalogApi, got {got}"
            raise RuntimeError(msg)

        return await api._service()

    def mount(self, app: FastAPI, router: APIRouter) -> None:
        setattr(app.state, self.STATE_KEY, self)
        CatalogHttp.install(app)
        catalog = APIRouter(prefix=CatalogUrl.PREFIX.value)
        routes = (
            (CatalogUrl.ACCESS, self.access, "GET"),
            (CatalogUrl.PROCESSES, self.list_processes, "GET"),
            (CatalogUrl.PROCESSES, self.create_process, "POST"),
            (CatalogUrl.PROCESSES_UPGRADE, self.upgrade_all, "POST"),
            (CatalogUrl.UPGRADES, self.upgrade_runs, "GET"),
            (CatalogUrl.UPGRADE, self.upgrade_run, "GET"),
            (CatalogUrl.UPGRADE, self.cancel_upgrade, "DELETE"),
            (CatalogUrl.UPGRADE_REPORT, self.upgrade_report, "GET"),
            (CatalogUrl.PROCESS, self.get_process, "GET"),
            (CatalogUrl.PROCESS_UPGRADE, self.last_upgrade, "GET"),
            (CatalogUrl.PROCESS_UPGRADE, self.upgrade_process, "POST"),
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
            (CatalogUrl.DRAFT_UPGRADE, self.last_draft_upgrade, "GET"),
            (CatalogUrl.DRAFT_UPGRADE, self.upgrade_draft, "POST"),
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
            catalog.add_api_route(
                path.value, handler, methods=[method], tags=[self.TAG]
            )

        router.include_router(catalog)

    async def access(
        self, identity: CurrentSubject, service: CurrentCatalog
    ) -> CatalogAccess:
        subject = identity.subject

        return service.access(subject)

    # --- процессы ---

    async def list_processes(
        self, identity: CurrentSubject, service: CurrentCatalog
    ) -> Sequence[Process]:
        subject = identity.subject

        return await service.list_processes(subject)

    async def create_process(
        self,
        body: ProcessSpec,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Process:
        subject = identity.subject

        return await service.create_process(subject, body)

    async def get_process(
        self, process_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Process:
        subject = identity.subject

        return await service.process(subject, process_id)

    async def update_process(
        self,
        process_id: UUID,
        body: ProcessSpec,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Process:
        subject = identity.subject

        return await service.update_process(subject, process_id, body)

    async def delete_process(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Deleted:
        subject = identity.subject

        deleted = await service.delete_process(subject, process_id)
        return Deleted(deleted=deleted)

    async def snapshot(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> CatalogSnapshot:
        subject = identity.subject

        return await service.snapshot(subject, process_id)

    async def versions(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sequence[Version]:
        subject = identity.subject

        return await service.versions(subject, process_id)

    async def context(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> ProcessContext:
        subject = identity.subject

        return await service.context(subject, process_id)

    async def staleness(
        self, process_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Staleness:
        subject = identity.subject

        return await service.staleness(subject, process_id)

    # --- черновики ---

    async def my_drafts(
        self, identity: CurrentSubject, service: CurrentCatalog
    ) -> Sequence[Draft]:
        """Открытые черновики пользователя по всем процессам."""
        subject = identity.subject

        return await service.my_drafts(subject)

    async def create_draft(
        self, body: DraftBody, identity: CurrentSubject, service: CurrentCatalog
    ) -> Draft:
        subject = identity.subject

        return await service.create_draft(subject, body.process_id, body.name)

    async def rename_draft(
        self,
        draft_id: UUID,
        body: DraftNameBody,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Draft:
        subject = identity.subject

        return await service.rename_draft(subject, draft_id, body.name)

    async def draft_state(
        self, draft_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> DraftState:
        subject = identity.subject

        return await service.draft_state(subject, draft_id)

    async def discard_draft(
        self, draft_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Draft:
        subject = identity.subject

        return await service.discard_draft(subject, draft_id)

    async def append_ops(
        self,
        draft_id: UUID,
        body: OpsBody,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> DraftState:
        subject = identity.subject

        return await service.append_ops(
            subject, draft_id, body.expected_seq, body.operations, AuthorVia.USER
        )

    async def publish(
        self, draft_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Version:
        subject = identity.subject

        return await service.publish(subject, draft_id, AuthorVia.USER)

    async def rebase(
        self,
        draft_id: UUID,
        body: RebaseBody,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> RebaseResult:
        subject = identity.subject

        return await service.rebase(
            subject, draft_id, drop_conflicts=body.drop_conflicts
        )

    async def draft_staleness(
        self,
        draft_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Staleness:
        subject = identity.subject

        return await service.draft_staleness(subject, draft_id)

    async def draft_context(
        self,
        draft_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> ProcessContext:
        subject = identity.subject

        return await service.draft_context(subject, draft_id)

    # --- upgrade: задача, как синхронизация ---

    async def upgrade_process(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> UpgradeRun:
        subject = identity.subject

        return await service.start_upgrade(
            subject, UpgradeTarget.PROCESS, process_id, AuthorVia.USER
        )

    async def upgrade_draft(
        self,
        draft_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> UpgradeRun:
        subject = identity.subject

        return await service.start_upgrade(
            subject, UpgradeTarget.DRAFT, draft_id, AuthorVia.USER
        )

    async def upgrade_all(
        self, identity: CurrentSubject, service: CurrentCatalog
    ) -> UpgradeRun:
        subject = identity.subject

        return await service.start_upgrade(
            subject, UpgradeTarget.ALL, None, AuthorVia.USER
        )

    async def upgrade_runs(
        self,
        identity: CurrentSubject,
        service: CurrentCatalog,
        process_id: UUID | None = None,
        draft_id: UUID | None = None,
        limit: int = Query(default=5, ge=1, le=50),
    ) -> Sequence[UpgradeRun]:
        subject = identity.subject

        return await service.upgrade_runs(subject, process_id, draft_id, limit)

    async def upgrade_run(
        self, run_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> UpgradeRun:
        subject = identity.subject

        return await service.upgrade_run(subject, run_id)

    async def cancel_upgrade(
        self,
        run_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> UpgradeRun:
        subject = identity.subject

        return await service.cancel_upgrade(subject, run_id)

    async def upgrade_report(
        self,
        run_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> UpgradeReport:
        subject = identity.subject

        return await service.upgrade_report(subject, run_id)

    async def last_upgrade(
        self, process_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Upgrade:
        subject = identity.subject

        upgrade = await service.last_upgrade(subject, process_id)
        if upgrade is None:
            msg = f"process {process_id} has no upgrades yet"
            raise HTTPException(status_code=404, detail=msg)

        return upgrade

    async def last_draft_upgrade(
        self,
        draft_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Upgrade:
        subject = identity.subject

        upgrade = await service.last_draft_upgrade(subject, draft_id)
        if upgrade is None:
            msg = f"draft {draft_id} has no upgrades yet"
            raise HTTPException(status_code=404, detail=msg)

        return upgrade

    # --- ссылки на просмотр ---

    async def shares(
        self,
        process_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sequence[Share]:
        subject = identity.subject

        return await service.shares(subject, process_id)

    async def share(
        self, process_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Share:
        subject = identity.subject

        return await service.share_process(subject, process_id)

    async def revoke_share(
        self, token: str, identity: CurrentSubject, service: CurrentCatalog
    ) -> Share:
        subject = identity.subject

        return await service.revoke_share(subject, token)

    async def shared(self, token: str, service: CurrentCatalog) -> SharedProcess:
        """Опубликованный процесс по ссылке: без входа и прав на каталог."""

        return await service.shared_process(token)

    async def shared_object(
        self, token: str, node_id: UUID, service: CurrentCatalog
    ) -> SerializeAsAny[ObjectCard]:

        return await service.shared_object(token, node_id)

    # --- подключения глазами каталога ---

    async def source_kinds(
        self, identity: CurrentSubject, service: CurrentCatalog
    ) -> Sequence[str]:
        """Виды подключений с установленным снимком: kind типов соединений;
        требует входа."""

        return service.source_kinds()

    async def synced_connections(
        self,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка: имя и вид на момент последнего снятия."""
        subject = identity.subject

        return await service.synced_connections(subject)

    async def connection_versions(
        self,
        connection_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sequence[ConnectionVersion]:
        subject = identity.subject

        return await service.connection_versions(subject, connection_id)

    async def write_connection_version(
        self,
        connection_id: UUID,
        body: SnapshotBody,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> ConnectionVersion:

        return await self._write_version(service, identity.subject, connection_id, body)

    async def forget_versions(
        self,
        connection_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Forgotten:
        """Все версии снимка подключения; отказ, пока оно стоит в узлах."""
        subject = identity.subject

        forgotten = await service.forget_versions(subject, connection_id)
        return Forgotten(versions=forgotten)

    async def connection_tree(
        self,
        connection_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
        version: int = LatestVersion.QUERY,
        path: Annotated[list[str], Query()] = [],  # noqa: B006
    ) -> Sequence[TreeNode]:
        subject = identity.subject

        return await service.connection_tree(subject, connection_id, version, path)

    async def connection_object(  # noqa: PLR0913 — параметры ресурса
        self,
        connection_id: UUID,
        kind: ObjectKind,
        path: Annotated[list[str], Query()],
        identity: CurrentSubject,
        service: CurrentCatalog,
        version: int = LatestVersion.QUERY,
    ) -> SerializeAsAny[ObjectCard]:
        subject = identity.subject

        ref = ObjectRef(connection_id=connection_id, kind=kind, path=tuple(path))
        return await service.connection_object(subject, ref, version)

    async def connection_diff(
        self,
        connection_id: UUID,
        old: int,
        new: int,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> SourceDiff:
        subject = identity.subject

        return await service.connection_diff(subject, connection_id, old, new)

    # --- синхронизации ---

    async def connection_syncs(
        self,
        connection_id: UUID,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sequence[Sync]:
        subject = identity.subject

        return await service.connection_syncs(subject, connection_id)

    async def start_sync(
        self,
        connection_id: UUID,
        body: SyncScope,
        identity: CurrentSubject,
        service: CurrentCatalog,
    ) -> Sync:
        """Синхронизация подключения инструментом вида от имени пользователя
        входа: возвращает запись сразу, ход виден по GET и событиям."""
        caller = SyncCaller.of_api(identity)

        return await service.start_sync(caller, connection_id, body)

    async def get_sync(
        self, sync_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Sync:
        subject = identity.subject

        return await service.sync(subject, sync_id)

    async def cancel_sync(
        self, sync_id: UUID, identity: CurrentSubject, service: CurrentCatalog
    ) -> Sync:
        subject = identity.subject

        return await service.cancel_sync(subject, sync_id)

    @staticmethod
    async def _write_version(
        service: CatalogService,
        subject: Subject,
        connection_id: UUID,
        body: SnapshotBody,
    ) -> ConnectionVersion:
        snapshot = service.parse_snapshot(body.snapshot)

        return await service.write_connection_version(subject, connection_id, snapshot)


CurrentCatalog = Annotated[CatalogService, Depends(CatalogApi.current)]


class CatalogHttp:
    """Перевод отказов сервиса каталога в HTTP-ответы: регистрируется
    обработчиками исключений приложения, маршруты ошибок не ловят."""

    STATUS: ClassVar[Mapping[type[Exception], int]] = {
        ProcessNotFoundError: 404,
        DraftNotFoundError: 404,
        ShareNotFoundError: 404,
        SharedNodeNotFoundError: 404,
        ConnectionNotSyncedError: 404,
        ConnectionVersionNotFoundError: 404,
        ObjectNotFoundError: 404,
        SyncNotFoundError: 404,
        UpgradeNotFoundError: 404,
        ProcessNameTakenError: 409,
        ConnectionInUseError: 409,
        SnapshotKindMismatchError: 409,
        SyncRunningError: 409,
        SyncClosedError: 409,
        DraftClosedError: 409,
        UpgradeClosedError: 409,
        UnknownSourceKindError: 422,
        SnapshotRejectedError: 422,
        SyncSetupError: 422,
        CatalogStoreError: 503,
    }

    @classmethod
    def install(cls, app: FastAPI) -> None:
        app.add_exception_handler(CatalogRefusalError, cls.refusal)
        app.add_exception_handler(CatalogServiceError, cls.service_error)
        app.add_exception_handler(CatalogOpError, cls.op_error)

    @classmethod
    async def refusal(cls, request: Request, exc: Exception) -> Response:
        if not isinstance(exc, CatalogRefusalError):
            raise exc

        if exc.refusal is CatalogRefusalKind.NOT_ALLOWED:
            return cls._reply(409, str(exc))

        return cls._reply(403, str(exc))

    @classmethod
    async def service_error(cls, request: Request, exc: Exception) -> Response:
        if isinstance(exc, DraftConflictError):
            detail = {"message": str(exc), "current_seq": exc.current_seq}
            return cls._reply(409, detail)

        if isinstance(exc, DraftStaleError):
            detail = {"message": str(exc), "current_version": exc.current_version}
            return cls._reply(409, detail)

        if isinstance(exc, CatalogStoreError):
            logger.error("catalog api: store failure: %s", exc)

        for error_type, status in cls.STATUS.items():
            if isinstance(exc, error_type):
                return cls._reply(status, str(exc))

        raise exc

    @classmethod
    async def op_error(cls, request: Request, exc: Exception) -> Response:
        if not isinstance(exc, CatalogOpError):
            raise exc

        detail = {"message": str(exc), "index": exc.index, "reason": exc.reason}
        return cls._reply(422, detail)

    @staticmethod
    def _reply(status: int, detail: str | Mapping[str, Any]) -> Response:
        return JSONResponse(status_code=status, content={"detail": detail})
