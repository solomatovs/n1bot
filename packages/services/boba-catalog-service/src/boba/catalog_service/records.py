"""Записи сервиса каталога: версии, черновики с порциями операций, виды,
раскладка, шаринг; ошибки слоя.

Ошибки:
CatalogServiceError — базовая ошибка сервиса каталога, наследники ниже.
CatalogStoreError — Postgres недоступен, ответ битый или строки таблиц не
    складываются в согласованный снимок.
DraftNotFoundError — черновика с таким id нет.
DraftClosedError — черновик уже опубликован или отброшен.
DraftConflictError — expected_seq отстал от черновика; current_seq — актуальный.
DraftStaleError — base_version черновика отстал от опубликованной версии;
    current_version — актуальная.
ViewNotFoundError — вида с таким id нет.
SourceNotFoundError — источника с таким id нет.
SourceObjectNotFoundError — по адресу в версии источника нет объекта.
SourceVersionNotFoundError — у источника нет версии с таким номером.
SourceDraftNotFoundError — черновика ручного источника с таким id нет.
SourceNotManualError — источник синхронизируемый, правки операциями закрыты.
CatalogRefusalError — у субъекта нет прав на действие; kind из CatalogRefusalKind.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from boba.catalog import (
    CatalogDiff,
    CatalogSnapshot,
    ObjectRef,
    OperationList,
    SourceDiff,
    SourceKind,
    SourceOperationList,
    SourceSnapshot,
)
from boba.identity.errors import RefusalError

__all__ = [
    "AuthorVia",
    "CatalogRefusalError",
    "CatalogRefusalKind",
    "CatalogServiceError",
    "CatalogStoreError",
    "Draft",
    "DraftAuthor",
    "DraftClosedError",
    "DraftConflictError",
    "DraftNotFoundError",
    "DraftOp",
    "DraftStaleError",
    "DraftState",
    "DraftStatus",
    "NodePosition",
    "RebaseIssue",
    "RebaseResult",
    "ShareMode",
    "ShareTargetKind",
    "Source",
    "SourceConnection",
    "SourceDraft",
    "SourceDraftNotFoundError",
    "SourceDraftOp",
    "SourceDraftState",
    "SourceNotFoundError",
    "SourceNotManualError",
    "SourceObjectNotFoundError",
    "SourceSpec",
    "SourceVersion",
    "SourceVersionNotFoundError",
    "Sync",
    "SyncStatus",
    "Version",
    "VersionOrigin",
    "View",
    "ViewLayout",
    "ViewNotFoundError",
    "ViewShare",
    "ViewSpec",
]


class CatalogServiceError(Exception):
    """Базовая ошибка сервиса каталога."""


class CatalogStoreError(CatalogServiceError):
    """База отказала, ответ битый или таблицы не складываются в снимок."""


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


class ViewNotFoundError(CatalogServiceError):
    """Вида с таким id нет."""

    def __init__(self, view_id: UUID) -> None:
        super().__init__(f"catalog: view {view_id} not found")
        self.view_id = view_id


class SourceNotFoundError(CatalogServiceError):
    def __init__(self, source_id: UUID) -> None:
        super().__init__(f"catalog: source {source_id} not found")
        self.source_id = source_id


class SourceVersionNotFoundError(CatalogServiceError):
    def __init__(self, source_id: UUID, version: int) -> None:
        super().__init__(f"catalog: source {source_id} has no version {version}")
        self.source_id = source_id
        self.version = version


class SourceDraftNotFoundError(CatalogServiceError):
    def __init__(self, draft_id: UUID) -> None:
        super().__init__(f"catalog: source draft {draft_id} not found")
        self.draft_id = draft_id


class SourceObjectNotFoundError(CatalogServiceError):
    def __init__(self, ref: ObjectRef) -> None:
        super().__init__(f"catalog: no {ref.kind.value} at {ref.render()}")
        self.ref = ref


class SourceNotManualError(CatalogServiceError):
    """Правки операциями открыты только ручному источнику."""

    def __init__(self, source_id: UUID) -> None:
        super().__init__(f"catalog: source {source_id} is synchronised, not manual")
        self.source_id = source_id


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


class Version(BaseModel):
    """Опубликованная версия: номер и свёрнутые операции черновика."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    operations: OperationList
    author: DraftAuthor
    published_at: datetime


class Draft(BaseModel):
    """Ветка правок над версией base_version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str = Field(min_length=1)
    base_version: int = Field(ge=0)
    status: DraftStatus
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


class RebaseIssue(BaseModel):
    """Операция черновика, не применимая к текущей версии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    index: int = Field(ge=0)
    reason: str = Field(min_length=1)


class RebaseResult(BaseModel):
    """Итог перебазирования: черновик и список конфликтов; пустой список —
    черновик переведён на текущую версию.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: Draft
    issues: tuple[RebaseIssue, ...]


class ShareTargetKind(StrEnum):
    ROLE = "role"
    USER = "user"


class ShareMode(StrEnum):
    VIEW = "view"


class ViewShare(BaseModel):
    """Кому открыт просмотр вида: роль по имени или пользователь по id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ShareTargetKind
    target: str = Field(min_length=1)
    mode: ShareMode = ShareMode.VIEW

    @classmethod
    def role(cls, name: str) -> ViewShare:
        return cls(kind=ShareTargetKind.ROLE, target=name)

    @classmethod
    def user(cls, user_id: UUID) -> ViewShare:
        return cls(kind=ShareTargetKind.USER, target=str(user_id))


class ViewSpec(BaseModel):
    """Имя вида и фильтр наборов и слоёв; пустой фильтр — весь каталог."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    dataset_ids: tuple[UUID, ...] = ()
    layer_ids: tuple[UUID, ...] = ()


class View(BaseModel):
    """Сохранённая диаграмма над каталогом."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str = Field(min_length=1)
    owner_id: UUID
    dataset_ids: tuple[UUID, ...]
    layer_ids: tuple[UUID, ...]
    created_at: datetime

    def spec(self) -> ViewSpec:
        return ViewSpec(
            name=self.name, dataset_ids=self.dataset_ids, layer_ids=self.layer_ids
        )


class NodePosition(BaseModel):
    """Положение узла набора на диаграмме вида."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: UUID
    x: float
    y: float


class ViewLayout(BaseModel):
    """Сохранённые позиции узлов вида; узлы без позиции раскладывает страница."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view_id: UUID
    positions: tuple[NodePosition, ...]


class ViewState(BaseModel):
    """Всё для страницы вида одним ответом: сам вид, номер текущей версии,
    срез опубликованного каталога по фильтру вида, сохранённая раскладка и
    признак, что вид принадлежит субъекту и он вправе его править."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    view: View
    version: int = Field(ge=0)
    snapshot: CatalogSnapshot
    layout: ViewLayout
    owned: bool


class CatalogAccess(BaseModel):
    """Права субъекта на каталог: страница по ним решает, что показывать."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    login: str
    can_view: bool
    can_edit: bool


class SourceSpec(BaseModel):
    """Что задаёт пользователь, заводя источник."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SourceKind
    name: str = Field(min_length=1)
    description: str = ""
    manual: bool = False


class Source(BaseModel):
    """Источник метаданных: форма снимка, имя, ручной или синхронизируемый."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    kind: SourceKind
    name: str = Field(min_length=1)
    description: str = ""
    manual: bool
    created_by: UUID
    created_at: datetime
    latest_version: int = Field(ge=0)

    def spec(self) -> SourceSpec:
        return SourceSpec(
            kind=self.kind,
            name=self.name,
            description=self.description,
            manual=self.manual,
        )


class SourceConnection(BaseModel):
    """Подключение брокера, привязанное к источнику."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    connection_id: UUID
    bound_by: UUID
    bound_at: datetime


class SourceVersion(BaseModel):
    """Снятая или опубликованная версия источника без самого снимка."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    version: int = Field(ge=1)
    taken_at: datetime
    taken_by: UUID
    connection_id: UUID | None = None
    sync_id: UUID | None = None
    objects_total: int = Field(ge=0)
    server_version: str | None = None


class VersionOrigin(BaseModel):
    """Откуда взялась версия: кто снимал, чем и в какой синхронизации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    taken_by: UUID
    connection_id: UUID | None = None
    sync_id: UUID | None = None
    server_version: str | None = None


class SyncStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Sync(BaseModel):
    """Синхронизация источника: прогресс и итог."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_id: UUID
    connection_id: UUID
    started_by: UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: SyncStatus
    scope: dict[str, object] = Field(default_factory=dict)
    objects_total: int | None = None
    objects_done: int = Field(ge=0, default=0)
    error: str | None = None
    version: int | None = None


class SourceDraft(BaseModel):
    """Черновик правок ручного источника над его версией base_version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_id: UUID
    name: str = Field(min_length=1)
    base_version: int = Field(ge=0)
    status: DraftStatus
    created_by: UUID
    created_at: datetime


class SourceDraftOp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: UUID
    seq: int = Field(ge=1)
    author: DraftAuthor
    operations: SourceOperationList
    created_at: datetime


class SourceDraftState(BaseModel):
    """Черновик ручного источника, свёрнутый в снимок, и его diff относительно
    базовой версии источника."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft: SourceDraft
    snapshot: SourceSnapshot = Field(discriminator="kind")
    diff: SourceDiff
    seq: int = Field(ge=0)
