"""Записи сервиса каталога: процессы с версиями и черновиками, ссылки на
просмотр, версии снимков подключений и синхронизации; ошибки слоя.

Ошибки:
CatalogServiceError — базовая ошибка сервиса каталога, наследники ниже.
CatalogStoreError — Postgres недоступен, ответ битый или строки таблиц не
    складываются в согласованный снимок.
ProcessNotFoundError — процесса с таким id нет.
ProcessNameTakenError — процесс с таким именем уже есть.
DraftNotFoundError — черновика с таким id нет.
DraftClosedError — черновик уже опубликован или отброшен.
DraftConflictError — expected_seq отстал от черновика; current_seq — актуальный.
DraftStaleError — base_version черновика отстал от опубликованной версии;
    current_version — актуальная.
ShareNotFoundError — ссылки с таким token нет или она отозвана.
SharedNodeNotFoundError — по ссылке запрошен узел, которого нет в процессе.
ConnectionNotSyncedError — у подключения нет ни одной версии снимка.
ConnectionVersionNotFoundError — у подключения нет версии с таким номером.
ConnectionInUseError — подключение стоит в узлах процессов; usage — где именно.
ConnectionHasVersionsError — у подключения есть версии снимка, их надо
    забыть раньше.
SnapshotKindMismatchError — снимок другого вида, чем прежние версии подключения.
ObjectNotFoundError — по адресу в версии снимка нет объекта; where — где
    искали, reason — ответ сборки карточки.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у подключения уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена, отменять нечего.
UnknownSourceKindError — у вида подключения нет снимка в реестре.
CatalogRefusalError — у субъекта нет прав на действие; kind из CatalogRefusalKind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from boba.catalog import (
    CatalogDiff,
    CatalogSnapshot,
    NodeColumn,
    ObjectRef,
    OperationList,
    SourceRecord,
    Staleness,
    SyncBatch,
)
from boba.identity.errors import RefusalError

__all__ = [
    "AuthorVia",
    "CatalogAccess",
    "CatalogRefusalError",
    "CatalogRefusalKind",
    "CatalogServiceError",
    "CatalogStoreError",
    "ConnectionHasVersionsError",
    "ConnectionInUseError",
    "ConnectionNotSyncedError",
    "ConnectionVersion",
    "ConnectionVersionNotFoundError",
    "Draft",
    "DraftAuthor",
    "DraftClosedError",
    "DraftConflictError",
    "DraftNotFoundError",
    "DraftOp",
    "DraftStaleError",
    "DraftState",
    "DraftStatus",
    "NodeUsage",
    "ObjectNotFoundError",
    "PinBump",
    "Process",
    "ProcessContext",
    "ProcessNameTakenError",
    "ProcessNotFoundError",
    "ProcessSpec",
    "RebaseIssue",
    "RebaseResult",
    "Share",
    "ShareNotFoundError",
    "SharedNodeNotFoundError",
    "SharedProcess",
    "SnapshotKindMismatchError",
    "StagedBatch",
    "Sync",
    "SyncClosedError",
    "SyncNotFoundError",
    "SyncRequest",
    "SyncRunningError",
    "SyncScope",
    "SyncStatus",
    "SyncedConnection",
    "UnknownSourceKindError",
    "Version",
    "VersionOrigin",
]


class CatalogServiceError(Exception):
    """Базовая ошибка сервиса каталога."""


class CatalogStoreError(CatalogServiceError):
    """База отказала, ответ битый или таблицы не складываются в снимок."""


class ProcessNotFoundError(CatalogServiceError):
    def __init__(self, process_id: UUID) -> None:
        super().__init__(f"catalog: process {process_id} not found")
        self.process_id = process_id


class ProcessNameTakenError(CatalogServiceError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"catalog: a process named {name!r} already exists; pick another name"
        )
        self.name = name


class DraftNotFoundError(CatalogServiceError):
    """Черновика с таким id нет."""

    def __init__(self, draft_id: UUID) -> None:
        super().__init__(f"catalog: draft {draft_id} not found")
        self.draft_id = draft_id


class DraftClosedError(CatalogServiceError):
    """Черновик уже опубликован или отброшен; порции и публикация невозможны."""

    def __init__(self, draft_id: UUID, status: DraftStatus) -> None:
        super().__init__(f"catalog: draft {draft_id} is {status.value}")
        self.draft_id = draft_id
        self.status = status


class DraftConflictError(CatalogServiceError):
    """Порция с отставшим expected_seq; клиент перечитывает черновик и повторяет."""

    def __init__(self, draft_id: UUID, expected_seq: int, current_seq: int) -> None:
        super().__init__(
            f"catalog: draft {draft_id} is at seq {current_seq}, "
            f"expected {expected_seq}"
        )
        self.draft_id = draft_id
        self.expected_seq = expected_seq
        self.current_seq = current_seq


class DraftStaleError(CatalogServiceError):
    """Черновик основан на устаревшей версии; нужен rebase."""

    def __init__(self, draft_id: UUID, base_version: int, current_version: int) -> None:
        super().__init__(
            f"catalog: draft {draft_id} is based on version {base_version}, "
            f"published is {current_version}"
        )
        self.draft_id = draft_id
        self.base_version = base_version
        self.current_version = current_version


class ShareNotFoundError(CatalogServiceError):
    def __init__(self, token: str) -> None:
        super().__init__(f"catalog: share link {token!r} not found or revoked")
        self.token = token


class SharedNodeNotFoundError(CatalogServiceError):
    """По ссылке запрошен узел, которого нет в опубликованном процессе."""

    def __init__(self, token: str, node_id: UUID) -> None:
        super().__init__(
            f"catalog: share link {token!r} has no node {node_id} in its process"
        )
        self.token = token
        self.node_id = node_id


class ConnectionNotSyncedError(CatalogServiceError):
    def __init__(self, connection_id: UUID) -> None:
        super().__init__(
            f"catalog: connection {connection_id} has no snapshot versions; "
            "sync it first"
        )
        self.connection_id = connection_id


class ConnectionVersionNotFoundError(CatalogServiceError):
    def __init__(self, connection_id: UUID, version: int) -> None:
        super().__init__(
            f"catalog: connection {connection_id} has no snapshot version {version}"
        )
        self.connection_id = connection_id
        self.version = version


class NodeUsage(BaseModel):
    """Сколько узлов процесса — опубликованных или в открытом черновике —
    стоит над объектами подключения; у черновика нового процесса process_id
    нет, имя процесса — имя черновика."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: UUID | None
    process_name: str
    draft: str = ""
    nodes: int = Field(ge=1)

    def render(self) -> str:
        if self.draft == "":
            return f"{self.nodes} node(s) of process {self.process_name!r}"

        return (
            f"{self.nodes} node(s) of draft {self.draft!r} "
            f"of process {self.process_name!r}"
        )


class ConnectionInUseError(CatalogServiceError):
    """Подключение стоит в узлах процессов: версии забыть нельзя."""

    def __init__(
        self, connection_id: UUID, name: str, usage: Sequence[NodeUsage]
    ) -> None:
        rendered: list[str] = []
        for entry in usage:
            rendered.append(entry.render())

        super().__init__(
            f"catalog: connection {name!r} ({connection_id}) is used by "
            f"{', '.join(rendered)}; remove these nodes first"
        )
        self.connection_id = connection_id
        self.name = name
        self.usage = tuple(usage)


class ConnectionHasVersionsError(CatalogServiceError):
    """У подключения есть версии снимка: удалять его нельзя, пока они не забыты."""

    def __init__(self, connection_id: UUID, name: str, versions: int) -> None:
        super().__init__(
            f"catalog: connection {name!r} ({connection_id}) has {versions} "
            "catalog version(s); forget them first"
        )
        self.connection_id = connection_id
        self.name = name
        self.versions = versions


class SnapshotKindMismatchError(CatalogServiceError):
    """Снимок другого вида, чем прежние версии подключения: они не сравнимы."""

    def __init__(self, connection_id: UUID, stored_kind: str, kind: str) -> None:
        super().__init__(
            f"catalog: connection {connection_id} has {stored_kind} snapshot "
            f"versions, the new snapshot is {kind}; forget the versions first"
        )
        self.connection_id = connection_id
        self.stored_kind = stored_kind
        self.kind = kind


class ObjectNotFoundError(CatalogServiceError):
    """По адресу нет объекта; where — где искали, reason — ответ сборки карточки."""

    def __init__(self, ref: ObjectRef, where: str, reason: str) -> None:
        super().__init__(f"catalog: {reason} ({where})")
        self.ref = ref
        self.where = where
        self.reason = reason


class SyncNotFoundError(CatalogServiceError):
    def __init__(self, sync_id: UUID) -> None:
        super().__init__(f"catalog: sync {sync_id} not found")
        self.sync_id = sync_id


class SyncRunningError(CatalogServiceError):
    def __init__(self, connection_id: UUID, sync_id: UUID) -> None:
        msg = (
            f"catalog: connection {connection_id} already has a running sync "
            f"{sync_id}; wait for it to finish or cancel it first"
        )
        super().__init__(msg)
        self.connection_id = connection_id
        self.sync_id = sync_id


class SyncClosedError(CatalogServiceError):
    def __init__(self, sync_id: UUID, status: SyncStatus) -> None:
        msg = f"catalog: sync {sync_id} is already {status.value}, nothing to cancel"
        super().__init__(msg)
        self.sync_id = sync_id
        self.status = status


class UnknownSourceKindError(CatalogServiceError):
    """Вида подключения нет в реестре снимков: пакет-владелец не установлен."""

    def __init__(self, kind: str, installed: Sequence[str]) -> None:
        super().__init__(
            f"catalog: connection kind {kind!r} has no snapshot installed, "
            f"installed kinds: {list(installed)}"
        )
        self.kind = kind
        self.installed = tuple(installed)


class CatalogRefusalKind(StrEnum):
    """Виды отказов сервиса каталога."""

    VIEW_FORBIDDEN = "catalog_view_forbidden"
    EDIT_FORBIDDEN = "catalog_edit_forbidden"
    NOT_OWNER = "catalog_not_owner"


class CatalogRefusalError(RefusalError):
    """У субъекта нет прав на действие; текст готов для пользователя и LLM."""

    def __init__(self, kind: CatalogRefusalKind, message: str) -> None:
        super().__init__(kind.value, message)
        self.refusal = kind


class AuthorVia(StrEnum):
    """Кем внесена порция: человеком со страницы или моделью из чата."""

    USER = "user"
    LLM = "llm"


class DraftAuthor(BaseModel):
    """От чьего имени и кем внесена порция или опубликована версия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    via: AuthorVia


class DraftStatus(StrEnum):
    OPEN = "open"
    PUBLISHED = "published"
    DISCARDED = "discarded"


class ProcessSpec(BaseModel):
    """Имя и описание процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""


class Process(BaseModel):
    """Процесс перетекания данных: свой рисунок с узлами, группами и потоками,
    своими версиями и черновиками; в списке — с числом узлов и открытых
    черновиков."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str = Field(min_length=1)
    description: str = ""
    owner_id: UUID
    created_at: datetime
    latest_version: int = Field(ge=0)
    nodes: int = Field(ge=0)
    open_drafts: int = Field(ge=0)

    def spec(self) -> ProcessSpec:
        return ProcessSpec(name=self.name, description=self.description)


class Version(BaseModel):
    """Опубликованная версия процесса: номер и свёрнутые операции черновика."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: UUID
    number: int = Field(ge=1)
    operations: OperationList
    author: DraftAuthor
    pins: Mapping[UUID, int] = Field(default_factory=dict)
    published_at: datetime


class Draft(BaseModel):
    """Ветка правок процесса над версией base_version; без process_id —
    черновик нового процесса над пустым снимком, имя станет именем процесса
    при публикации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    process_id: UUID | None
    name: str = Field(min_length=1)
    base_version: int = Field(ge=0)
    status: DraftStatus
    pins: Mapping[UUID, int] = Field(default_factory=dict)
    created_by: UUID
    created_at: datetime


class DraftOp(BaseModel):
    """Порция операций черновика с порядковым номером seq."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: UUID
    seq: int = Field(ge=1)
    author: DraftAuthor
    operations: OperationList
    created_at: datetime


class DraftState(BaseModel):
    """Черновик, свёрнутый в снимок, и его diff относительно базовой версии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: Draft
    snapshot: CatalogSnapshot
    diff: CatalogDiff
    seq: int = Field(ge=0)


class ProcessContext(BaseModel):
    """Что процессу нужно от снимков подключений для показа: привязки версий,
    колонки каждого узла из привязанной версии и устаревание относительно
    последних версий. Считается для опубликованной версии или черновика."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pins: Mapping[UUID, int]
    columns: Mapping[UUID, tuple[NodeColumn, ...]]
    stale: Staleness


class PinBump(BaseModel):
    """Черновик после поднятия привязок и что в нём перестало сходиться."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: Draft
    violations: tuple[str, ...]


class RebaseIssue(BaseModel):
    """Операция черновика, не применимая к текущей версии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    index: int = Field(ge=0)
    reason: str = Field(min_length=1)


class RebaseResult(BaseModel):
    """Итог перебазирования: черновик и список конфликтов; пустой список —
    черновик переведён на текущую версию."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: Draft
    issues: tuple[RebaseIssue, ...]


class Share(BaseModel):
    """Ссылка на просмотр опубликованного процесса: гость видит его по token
    без прав на каталог, пока ссылка не отозвана."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str = Field(min_length=1)
    process_id: UUID
    created_by: UUID
    created_at: datetime
    revoked_at: datetime | None = None


class SharedProcess(BaseModel):
    """Опубликованный процесс по ссылке на просмотр: сам процесс, его снимок и
    контекст показа; прав на каталог у читателя нет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process: Process
    snapshot: CatalogSnapshot
    context: ProcessContext


class CatalogAccess(BaseModel):
    """Права субъекта на каталог: страница по ним решает, что показывать."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    login: str
    can_view: bool
    can_edit: bool


class SyncedConnection(BaseModel):
    """Подключение глазами каталога: имя и вид из последней версии снимка,
    номер последней версии и когда она снята."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    latest_version: int = Field(ge=1)
    synced_at: datetime


class ConnectionVersion(BaseModel):
    """Снятая версия снимка подключения без самого снимка; имя и вид
    подключения — копия на момент снятия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    version: int = Field(ge=1)
    connection_name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    taken_at: datetime
    taken_by: UUID
    sync_id: UUID | None = None
    objects_total: int = Field(ge=0)
    server_version: str | None = None


class VersionOrigin(BaseModel):
    """Откуда взялась версия: кто снимал, как звалось подключение, в какой
    синхронизации и с какого сервера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    taken_by: UUID
    connection_name: str = Field(min_length=1)
    sync_id: UUID | None = None
    server_version: str | None = None


class SyncStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SyncScope(BaseModel):
    """Что и как снимать: схемы (пусто — все несистемные), размер порции и
    пауза между заходами инструмента в каталог базы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: tuple[str, ...] = ()
    batch_size: int = Field(ge=1, le=10_000, default=200)
    pause_ms: int = Field(ge=0, le=60_000, default=0)

    def schemas_arg(self) -> str:
        """Схемы одной строкой, как их принимает инструмент снятия."""
        return ", ".join(self.schemas)


class SyncRequest(BaseModel):
    """Что синхронизировать: подключение с именем и видом на момент запуска
    и охват."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    connection_name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    scope: SyncScope = Field(default_factory=SyncScope)


class StagedBatch(BaseModel):
    """Порция синхронизации из staging: заголовок и записи её части."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch: SyncBatch
    records: tuple[SerializeAsAny[SourceRecord], ...]


class Sync(BaseModel):
    """Синхронизация подключения: прогресс и итог."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    connection_id: UUID
    connection_name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    started_by: UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: SyncStatus
    scope: SyncScope = Field(default_factory=SyncScope)
    objects_total: int | None = None
    objects_done: int = Field(ge=0, default=0)
    error: str | None = None
    version: int | None = None
