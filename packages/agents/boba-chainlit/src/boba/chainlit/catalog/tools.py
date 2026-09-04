"""Инструменты LLM над каталогом данных: живут на хосте и зовут CatalogService
от имени субъекта хода чата.

catalog_read отдаёт модели снимок или срез по наборам с соседями по потокам и
видами загрузки; catalog_draft создаёт черновик или перечисляет открытые;
catalog_propose шлёт порцию операций JSON-списком; catalog_diff показывает
черновик относительно его базовой версии; catalog_open оставляет в чате
ссылку на страницу черновика или вида.

Ошибки: ErrorResult — нет хода чата, нет прав, черновик или вид не найден,
операции не разбираются или не применимы, хранилище недоступно; остальное
упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from operator import attrgetter
from typing import Annotated, Any, ClassVar
from uuid import UUID

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    Dataset,
    EntityRef,
    Flow,
    OperationList,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogService,
    CatalogServiceError,
    Draft,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftStaleError,
    DraftState,
    ViewNotFoundError,
)
from boba.identity.context import CallContext, Subject
from boba.identity.errors import RefusalError
from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.failure import ValidationText
from boba.toolkit.result import (
    CustomElementResult,
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
    ToolResult,
    pack_result,
)

__all__ = [
    "CatalogLinkKind",
    "CatalogPageUrl",
    "CatalogToolConfig",
    "CatalogToolError",
    "CatalogTools",
    "CatalogView",
    "DiffReport",
    "build_catalog_tools",
]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[CatalogService]]
PrefixSource = Callable[[], str]


class CatalogToolConfig(BaseModel):
    """Секция [tool.catalog]: своих параметров у инструментов нет."""

    model_config = ConfigDict(extra="ignore")


class CatalogToolError(StrEnum):
    """Виды отказов инструментов каталога в ErrorResult."""

    NOT_FOUND = "catalog_not_found"
    DRAFT_CLOSED = "catalog_draft_closed"
    DRAFT_CONFLICT = "catalog_draft_conflict"
    DRAFT_STALE = "catalog_draft_stale"
    BAD_OPERATIONS = "catalog_bad_operations"
    OPERATION_REJECTED = "catalog_operation_rejected"
    BAD_ID = "catalog_bad_id"
    STORE = "catalog_store_error"


class CatalogLinkKind(StrEnum):
    """Что открывает ссылка каталога в чате."""

    DRAFT = "draft"
    VIEW = "view"


class CatalogPageUrl(StrEnum):
    """Адреса страницы каталога относительно префикса приложения."""

    DRAFT = "/catalog/drafts/{draft_id}"
    VIEW = "/catalog/views/{view_id}"

    @classmethod
    def draft(cls, prefix: str, draft_id: UUID) -> str:
        return prefix + cls.DRAFT.value.format(draft_id=draft_id)

    @classmethod
    def view(cls, prefix: str, view_id: UUID) -> str:
        return prefix + cls.VIEW.value.format(view_id=view_id)


class CatalogPrompt(StrEnum):
    """Тексты фасада инструментов для модели."""

    DATASETS = (
        "Comma-separated dataset names to focus on; empty string returns the whole "
        "catalog. With names the answer holds those datasets, their columns, the "
        "flows touching them and the datasets on the other end of those flows."
    )
    DRAFT_NAME = (
        "Name of a new draft to create; empty string lists the open drafts instead. "
        "A draft is a branch of operations over the published catalog: propose "
        "changes into it, then the user publishes it from the page."
    )
    DRAFT_ID = "Draft id (uuid) from catalog_draft."
    OPERATIONS = (
        'JSON array of operations. Each item has "op" and a body: add_layer/'
        "set_layer {layer}, remove_layer {id}; add_dataset/set_dataset {dataset}, "
        "remove_dataset {id}; add_column/set_column {column}, remove_column {id}; "
        "add_load_kind/set_load_kind {load_kind}, remove_load_kind {id}; add_flow/"
        "set_flow {flow}, remove_flow {id}. Entities carry their own uuid ids: "
        "generate new uuids for add_*, reuse existing ids for set_* (the whole "
        "entity is replaced) and remove_*. Flow load values follow the fields of "
        "its load kind; column references are column ids. Removing a dataset "
        "removes its columns but is refused while flows use it: remove the flows "
        "earlier in the same list."
    )
    LINK_KIND = "What to open: 'draft' or 'view'."
    LINK_ID = "Id (uuid) of the draft or the view."
    OPENED_NOTE = "the link stays in the chat and opens the catalog page"


class ColumnView(BaseModel):
    """Колонка набора глазами модели."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    type: str
    nullable: bool
    is_key: bool
    position: int
    description: str


class DatasetView(BaseModel):
    """Набор с именем слоя и колонками."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    layer: str
    layer_id: UUID
    name: str
    source: str
    description: str
    tags: tuple[str, ...]
    owner: str
    columns: tuple[ColumnView, ...]


class FlowView(BaseModel):
    """Поток с именами концов и правилом загрузки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    from_dataset: str
    from_dataset_id: UUID
    to_dataset: str
    to_dataset_id: UUID
    load_kind: str
    load_kind_id: UUID
    load_values: Mapping[str, Any]
    description: str


class LoadKindView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    description: str
    fields: tuple[Mapping[str, Any], ...]


class LayerView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str


class CatalogView(BaseModel):
    """Снимок или его срез в форме, удобной модели: имена рядом с id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    layers: tuple[LayerView, ...]
    load_kinds: tuple[LoadKindView, ...]
    datasets: tuple[DatasetView, ...]
    flows: tuple[FlowView, ...]
    unknown_datasets: tuple[str, ...]

    @classmethod
    def of(
        cls, snapshot: CatalogSnapshot, version: int, names: Sequence[str]
    ) -> CatalogView:
        """Весь каталог без имён; с именами — эти наборы, их потоки и соседи."""
        chosen, unknown = cls._chosen(snapshot, names)

        flows: list[FlowView] = []
        for flow in snapshot.flows.values():
            if not cls._touches(flow, chosen):
                continue

            chosen.add(flow.from_dataset_id)
            chosen.add(flow.to_dataset_id)
            flows.append(cls._flow(snapshot, flow))

        datasets: list[DatasetView] = []
        for dataset in snapshot.datasets.values():
            if dataset.id not in chosen:
                continue

            datasets.append(cls._dataset(snapshot, dataset))

        layers: list[LayerView] = []
        for layer in snapshot.layers.values():
            layers.append(LayerView(id=layer.id, name=layer.name))

        kinds: list[LoadKindView] = []
        for kind in snapshot.load_kinds.values():
            fields = kind.model_dump(mode="json")["fields"]
            kinds.append(
                LoadKindView(
                    id=kind.id,
                    name=kind.name,
                    description=kind.description,
                    fields=tuple(fields),
                )
            )

        return cls(
            version=version,
            layers=tuple(layers),
            load_kinds=tuple(kinds),
            datasets=tuple(datasets),
            flows=tuple(flows),
            unknown_datasets=tuple(unknown),
        )

    @staticmethod
    def _chosen(
        snapshot: CatalogSnapshot, names: Sequence[str]
    ) -> tuple[set[UUID], list[str]]:
        """Наборы по именам; без имён выбран весь каталог."""
        if not names:
            return set(snapshot.datasets), []

        chosen: set[UUID] = set()
        unknown: list[str] = []
        for name in names:
            found = False
            for dataset in snapshot.datasets.values():
                if dataset.name != name:
                    continue

                chosen.add(dataset.id)
                found = True

            if not found:
                unknown.append(name)

        return chosen, unknown

    @staticmethod
    def _touches(flow: Flow, chosen: set[UUID]) -> bool:
        if flow.from_dataset_id in chosen:
            return True

        return flow.to_dataset_id in chosen

    @staticmethod
    def _dataset(snapshot: CatalogSnapshot, dataset: Dataset) -> DatasetView:
        layer_name = snapshot.layers[dataset.layer_id].name

        columns: list[ColumnView] = []
        for column in sorted(
            snapshot.columns_of(dataset.id), key=attrgetter("position")
        ):
            columns.append(ColumnView.model_validate(column.model_dump()))

        return DatasetView(
            id=dataset.id,
            layer=layer_name,
            layer_id=dataset.layer_id,
            name=dataset.name,
            source=dataset.source,
            description=dataset.description,
            tags=dataset.tags,
            owner=dataset.owner,
            columns=tuple(columns),
        )

    @staticmethod
    def _flow(snapshot: CatalogSnapshot, flow: Flow) -> FlowView:
        kind = snapshot.load_kinds[flow.load.kind_id]
        values = flow.load.model_dump(mode="json")["values"]

        return FlowView(
            id=flow.id,
            from_dataset=snapshot.datasets[flow.from_dataset_id].name,
            from_dataset_id=flow.from_dataset_id,
            to_dataset=snapshot.datasets[flow.to_dataset_id].name,
            to_dataset_id=flow.to_dataset_id,
            load_kind=kind.name,
            load_kind_id=kind.id,
            load_values=values,
            description=flow.description,
        )


class DiffReport:
    """Текст diff черновика для модели: статусы с подписями сущностей."""

    def __init__(self, state: DraftState) -> None:
        self._state = state

    def render(self) -> str:
        lines = list(self._lines())
        header = (
            f"draft {self._state.draft.name!r} ({self._state.draft.id}) at seq "
            f"{self._state.seq} over version {self._state.draft.base_version}: "
            f"{len(lines)} change(s)"
        )
        if not lines:
            return header

        return header + "\n" + "\n".join(lines)

    def _lines(self) -> Iterator[str]:
        for entry in self._state.diff.entries:
            yield f"{entry.status.value} {self._label(entry.ref, entry.status)}"

    def _label(self, ref: EntityRef, status: ChangeStatus) -> str:
        if status is ChangeStatus.REMOVED:
            return f"{ref.kind.value} {ref.id}"

        return self._state.snapshot.label(ref)


class CatalogTools:
    """Тела инструментов каталога: субъект из хода чата, ответы моделям."""

    APPEND_ATTEMPTS: ClassVar[int] = 3
    """Порция повторяется с перечитанным seq, если параллельный автор опередил."""

    LINK_ELEMENT: ClassVar[str] = "CatalogLink"
    """Имя jsx-компонента ссылки: public/elements/CatalogLink.jsx."""

    def __init__(self, service: ServiceSource, prefix: PrefixSource) -> None:
        self._service = service
        self._prefix = prefix

    async def read(self, datasets: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            service = await self._service()
            snapshot = await service.snapshot(subject)
            version = await service.store.current_version()
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        names = self._names(datasets)
        view = CatalogView.of(snapshot, version, names)

        return pack_result(JsonResult(payload=view.model_dump(mode="json")))

    async def draft(self, name: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            service = await self._service()
            if not name.strip():
                drafts = await service.open_drafts(subject)
                return pack_result(self._drafts_table(drafts))

            created = await service.create_draft(subject, name.strip())
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        text = (
            f"draft created: {created.id} ({created.name!r}) over version "
            f"{created.base_version}; propose operations with catalog_propose"
        )
        return pack_result(
            TextResult(text=text, metadata={"draft_id": str(created.id)})
        )

    async def propose(self, draft_id: str, operations: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            parsed_id = self._uuid(draft_id)
            ops = self._operations(operations)
            service = await self._service()
            state = await self._append(service, subject, parsed_id, ops)
        except (RefusalError, CatalogServiceError, CatalogOpError) as exc:
            return pack_result(self._error(exc))

        return pack_result(
            TextResult(
                text=DiffReport(state).render(), metadata={"seq": str(state.seq)}
            )
        )

    async def diff(self, draft_id: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            parsed_id = self._uuid(draft_id)
            service = await self._service()
            state = await service.draft_state(subject, parsed_id)
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        return pack_result(TextResult(text=DiffReport(state).render()))

    async def open(self, kind: str, entity_id: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            link_kind = self._link_kind(kind)
            parsed_id = self._uuid(entity_id)
            service = await self._service()
            label, url = await self._target(service, subject, link_kind, parsed_id)
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        link = CustomElementResult(
            element=self.LINK_ELEMENT,
            props={"url": url, "label": label, "kind": link_kind.value},
            title=label,
        )
        content = (
            f"opened {link_kind.value} {label!r}: {url}; {CatalogPrompt.OPENED_NOTE}"
        )

        return content, link

    async def _append(
        self,
        service: CatalogService,
        subject: Subject,
        draft_id: UUID,
        ops: OperationList,
    ) -> DraftState:
        """Порция с актуальным seq; конфликт с параллельным автором — повтор."""
        attempt = 0
        while True:
            attempt += 1
            current = await service.draft_state(subject, draft_id)
            try:
                return await service.append_ops(
                    subject, draft_id, current.seq, ops, AuthorVia.LLM
                )
            except DraftConflictError:
                if attempt >= self.APPEND_ATTEMPTS:
                    raise

    async def _target(
        self,
        service: CatalogService,
        subject: Subject,
        kind: CatalogLinkKind,
        entity_id: UUID,
    ) -> tuple[str, str]:
        prefix = self._prefix()
        if kind is CatalogLinkKind.DRAFT:
            state = await service.draft_state(subject, entity_id)
            return state.draft.name, CatalogPageUrl.draft(prefix, entity_id)

        view = await service.view(subject, entity_id)
        return view.name, CatalogPageUrl.view(prefix, entity_id)

    @staticmethod
    def _subject() -> Subject:
        """Субъект текущего вызова; вне хода — RefusalError."""
        return CallContext.current_subject()

    @staticmethod
    def _uuid(raw: str) -> UUID:
        try:
            return UUID(raw.strip())
        except ValueError as exc:
            msg = f"not a uuid: {raw!r}"
            raise RefusalError(CatalogToolError.BAD_ID.value, msg) from exc

    @staticmethod
    def _link_kind(raw: str) -> CatalogLinkKind:
        try:
            return CatalogLinkKind(raw.strip().lower())
        except ValueError as exc:
            msg = f"kind must be 'draft' or 'view', got {raw!r}"
            raise RefusalError(CatalogToolError.BAD_ID.value, msg) from exc

    @staticmethod
    def _operations(raw: str) -> OperationList:
        try:
            return OperationList.model_validate_json(raw)
        except ValidationError as exc:
            details = ValidationText.of(exc)
            msg = f"operations do not parse: {details}"
            raise RefusalError(CatalogToolError.BAD_OPERATIONS.value, msg) from exc

    @staticmethod
    def _names(raw: str) -> list[str]:
        names: list[str] = []
        for part in raw.split(","):
            name = part.strip()
            if not name:
                continue

            names.append(name)

        return names

    @staticmethod
    def _drafts_table(drafts: Sequence[Draft]) -> ToolResult:
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            rows.append(
                {
                    "draft_id": str(draft.id),
                    "name": draft.name,
                    "base_version": draft.base_version,
                    "created_at": draft.created_at.isoformat(timespec="seconds"),
                }
            )

        if not rows:
            return TextResult(text="no open drafts; create one with catalog_draft")

        return TableResult(rows=rows)

    ERROR_KINDS: ClassVar[tuple[tuple[type[Exception], CatalogToolError], ...]] = (
        (DraftNotFoundError, CatalogToolError.NOT_FOUND),
        (ViewNotFoundError, CatalogToolError.NOT_FOUND),
        (DraftClosedError, CatalogToolError.DRAFT_CLOSED),
        (DraftStaleError, CatalogToolError.DRAFT_STALE),
    )
    """Ошибки сервиса, у которых виду отказа хватает текста самой ошибки."""

    @classmethod
    def _error(cls, exc: Exception) -> ErrorResult:
        """Отказы сервиса и хранилища — в ErrorResult с видом отказа."""
        if isinstance(exc, RefusalError):
            return ErrorResult(message=str(exc), error_kind=exc.kind)

        if isinstance(exc, DraftConflictError):
            return ErrorResult(
                message=f"{exc}; re-read the draft and propose again",
                error_kind=CatalogToolError.DRAFT_CONFLICT,
            )

        if isinstance(exc, CatalogOpError):
            message = (
                f"operation #{exc.index} ({exc.op.op.value}) was rejected: "
                f"{exc.reason}; nothing from this list was applied"
            )
            return ErrorResult(
                message=message, error_kind=CatalogToolError.OPERATION_REJECTED
            )

        for error_type, kind in cls.ERROR_KINDS:
            if isinstance(exc, error_type):
                return ErrorResult(message=str(exc), error_kind=kind)

        logger.error("catalog tool: %s", exc)
        return ErrorResult(message=str(exc), error_kind=CatalogToolError.STORE)


def build_catalog_tools(
    cfg: CatalogToolConfig, service: ServiceSource, prefix: PrefixSource
) -> list[BaseTool]:
    tools = CatalogTools(service, prefix)
    ToolCallViews.register("catalog_propose", ScriptCall(arg="operations", lang="json"))

    @tool(response_format="content_and_artifact")
    async def catalog_read(
        datasets: Annotated[str, Field(description=CatalogPrompt.DATASETS)],
    ) -> tuple[str, ToolResult]:
        """Read the published data catalog: layers, datasets with columns, load
        kinds with their fields and flows between datasets. Call it before
        proposing changes to learn the existing ids, names and load kinds."""
        return await tools.read(datasets)

    @tool(response_format="content_and_artifact")
    async def catalog_draft(
        name: Annotated[str, Field(description=CatalogPrompt.DRAFT_NAME)],
    ) -> tuple[str, ToolResult]:
        """Create a catalog draft by name or list the open drafts (empty name).
        Changes go into a draft first; the user reviews and publishes it on
        the catalog page."""
        return await tools.draft(name)

    @tool(response_format="content_and_artifact")
    async def catalog_propose(
        draft_id: Annotated[
            str, Field(min_length=1, description=CatalogPrompt.DRAFT_ID)
        ],
        operations: Annotated[
            str, Field(min_length=1, description=CatalogPrompt.OPERATIONS)
        ],
    ) -> tuple[str, ToolResult]:
        """Append a list of catalog operations to a draft. The list is applied
        atomically: one rejected operation rejects the whole list with its
        index and reason. The answer is the draft diff against the published
        version."""
        return await tools.propose(draft_id, operations)

    @tool(response_format="content_and_artifact")
    async def catalog_diff(
        draft_id: Annotated[
            str, Field(min_length=1, description=CatalogPrompt.DRAFT_ID)
        ],
    ) -> tuple[str, ToolResult]:
        """Show what a draft changes against the published catalog: added,
        modified and removed entities."""
        return await tools.diff(draft_id)

    @tool(response_format="content_and_artifact")
    async def catalog_open(
        kind: Annotated[str, Field(min_length=1, description=CatalogPrompt.LINK_KIND)],
        entity_id: Annotated[
            str, Field(min_length=1, description=CatalogPrompt.LINK_ID)
        ],
    ) -> tuple[str, ToolResult]:
        """Put a link to the catalog page of a draft or a view into the chat so
        the user can open the diagram."""
        return await tools.open(kind, entity_id)

    return [catalog_read, catalog_draft, catalog_propose, catalog_diff, catalog_open]
