"""Инструменты LLM над каталогом данных: живут на хосте и зовут CatalogService
от имени субъекта хода чата.

catalog_read перечисляет процессы или отдаёт модели снимок одного процесса
либо срез по узлам с колонками из снимков подключений и соседями по потокам;
catalog_draft создаёт черновик процесса или перечисляет открытые;
catalog_propose шлёт порцию операций JSON-списком; catalog_diff показывает
черновик относительно его базовой версии; catalog_open оставляет в чате
ссылку на страницу процесса или черновика; catalog_sync снимает структуру
подключения инструментом его вида и ждёт итога.

Ошибки: ErrorResult — нет хода чата, нет прав, процесс или черновик не
найден, операции не разбираются или не применимы, хранилище недоступно;
остальное упаковывает ToolErrorGuard.
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
    EntityRef,
    Flow,
    Node,
    ObjectResolver,
    OperationList,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogService,
    CatalogServiceError,
    ConnectionInfo,
    Draft,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftStaleError,
    DraftState,
    Process,
    ProcessNotFoundError,
    SnapshotKindMismatchError,
    SyncCaller,
    SyncRunningError,
    SyncScope,
    SyncSetupError,
    SyncStatus,
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
    SYNC_REFUSED = "catalog_sync_refused"


class CatalogLinkKind(StrEnum):
    """Что открывает ссылка каталога в чате."""

    DRAFT = "draft"
    PROCESS = "process"


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


class CatalogPrompt(StrEnum):
    """Тексты фасада инструментов для модели."""

    PROCESS = (
        "Process: its name or id (uuid); empty string lists the processes "
        "with their ids, versions, node counts and open drafts."
    )
    NODES = (
        "Comma-separated node labels to focus on (object name or alias); empty "
        "string returns the whole process. With labels the answer holds those "
        "nodes with their columns, the flows touching them and the nodes on the "
        "other end of those flows."
    )
    DRAFT_PROCESS = (
        "Process the draft belongs to: its name or id (uuid); empty string "
        "starts a draft of a new process, which gets the draft's name when "
        "published."
    )
    DRAFT_NAME = (
        "Name of a new draft to create; empty string lists the user's open "
        "drafts instead (of that process, or of every process when the process "
        "is empty too). A draft is a branch of operations over the published "
        "process: propose changes into it, then the user publishes it from the "
        "page."
    )
    DRAFT_ID = "Draft id (uuid) from catalog_draft."
    OPERATIONS = (
        'JSON array of operations. Each item has "op" and a body: add_node/'
        "set_node {node: {id, ref: {connection_id, kind, path[]}, position: "
        "{x, y} or null, width: card width in px or null, group_id: uuid or "
        "null, alias, note}}, retarget_node "
        "{id, ref}, remove_node {id}; add_group/set_group {group: {id, name}}, "
        "remove_group {id}; "
        "add_flow/set_flow {flow: {id, from_node_id, to_node_id, columns: "
        "[{from_column, to_column}], description}}, remove_flow {id}. Entities "
        "carry their own uuid ids: generate new uuids for add_*, reuse existing "
        "ids for set_* (the whole entity is replaced) and remove_*. A node points "
        "at an object of a synced connection by its address (kind and path); a "
        "flow says which columns of the source node go into which columns of "
        "the target node, by their names from catalog_read; several source "
        "columns may go into one target column and vice versa. Nodes are free "
        "on the canvas: position and width are optional (the page lays out "
        "nodes without a position and draws cards of the default width) and a "
        "group is an optional named frame around nodes. Removing a "
        "node is refused while flows use it, removing a group while nodes are "
        "in it: remove or move them earlier in the same list."
    )
    LINK_KIND = "What to open: 'process' or 'draft'."
    LINK_ID = "Id (uuid) of the process or the draft."
    OPENED_NOTE = "the link stays in the chat and opens the catalog page"
    SYNC_CONNECTION = "Connection to snapshot: its name or id (uuid)."
    SYNC_SCHEMAS = (
        "Comma-separated schemas to snapshot; empty string takes every "
        "non-system schema of the database."
    )


class ColumnView(BaseModel):
    """Колонка объекта глазами модели: имя как в базе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class PositionView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float
    y: float


class NodeView(BaseModel):
    """Узел с позицией, группой, адресом объекта и колонками из версии снимка."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    position: PositionView | None
    width: float | None
    group: str | None
    group_id: UUID | None
    label: str
    alias: str | None
    note: str
    connection_id: UUID
    object_kind: str
    path: tuple[str, ...]
    columns: tuple[ColumnView, ...]


class ColumnLinkView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_column: str
    to_column: str


class FlowView(BaseModel):
    """Поток с подписями концов и парами колонок."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    from_node: str
    from_node_id: UUID
    to_node: str
    to_node_id: UUID
    columns: tuple[ColumnLinkView, ...]
    description: str


class GroupView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str


class CatalogView(BaseModel):
    """Снимок процесса или его срез в форме, удобной модели: подписи рядом с id,
    колонки узлов из привязанных версий снимков."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: UUID
    process: str
    version: int
    pins: Mapping[str, int]
    groups: tuple[GroupView, ...]
    nodes: tuple[NodeView, ...]
    flows: tuple[FlowView, ...]
    unknown_nodes: tuple[str, ...]

    @classmethod
    def of(
        cls,
        process: Process,
        snapshot: CatalogSnapshot,
        pins: Mapping[UUID, int],
        resolver: ObjectResolver,
        labels: Sequence[str],
    ) -> CatalogView:
        """Весь процесс без подписей; с подписями — эти узлы, их потоки и соседи."""
        chosen, unknown = cls._chosen(snapshot, labels)

        flows: list[FlowView] = []
        for flow in snapshot.flows.values():
            if not cls._touches(flow, chosen):
                continue

            chosen.add(flow.from_node_id)
            chosen.add(flow.to_node_id)
            flows.append(cls._flow(snapshot, flow))

        nodes: list[NodeView] = []
        for node in snapshot.nodes.values():
            if node.id not in chosen:
                continue

            nodes.append(cls._node(snapshot, node, resolver))

        groups: list[GroupView] = []
        for group in sorted(snapshot.groups.values(), key=attrgetter("name")):
            groups.append(GroupView(id=group.id, name=group.name))

        rendered_pins: dict[str, int] = {}
        for connection_id, pinned in pins.items():
            rendered_pins[str(connection_id)] = pinned

        return cls(
            process_id=process.id,
            process=process.name,
            version=process.latest_version,
            pins=rendered_pins,
            groups=tuple(groups),
            nodes=tuple(nodes),
            flows=tuple(flows),
            unknown_nodes=tuple(unknown),
        )

    @staticmethod
    def _chosen(
        snapshot: CatalogSnapshot, labels: Sequence[str]
    ) -> tuple[set[UUID], list[str]]:
        """Узлы по подписям; без подписей выбран весь процесс."""
        if not labels:
            return set(snapshot.nodes), []

        chosen: set[UUID] = set()
        unknown: list[str] = []
        for label in labels:
            found = False
            for node in snapshot.nodes.values():
                if label not in (node.label, node.ref.path[-1], node.ref.render()):
                    continue

                chosen.add(node.id)
                found = True

            if not found:
                unknown.append(label)

        return chosen, unknown

    @staticmethod
    def _touches(flow: Flow, chosen: set[UUID]) -> bool:
        if flow.from_node_id in chosen:
            return True

        return flow.to_node_id in chosen

    @staticmethod
    def _node(
        snapshot: CatalogSnapshot, node: Node, resolver: ObjectResolver
    ) -> NodeView:
        group_name = None
        if node.group_id is not None:
            group_name = snapshot.groups[node.group_id].name

        position = None
        if node.position is not None:
            position = PositionView(x=node.position.x, y=node.position.y)

        columns: list[ColumnView] = []
        names = resolver.columns_of(node.ref)
        if names is not None:
            for name in names:
                columns.append(ColumnView(name=name))

        return NodeView(
            id=node.id,
            position=position,
            width=node.width,
            group=group_name,
            group_id=node.group_id,
            label=node.label,
            alias=node.alias,
            note=node.note,
            connection_id=node.ref.connection_id,
            object_kind=node.ref.kind.value,
            path=node.ref.path,
            columns=tuple(columns),
        )

    @staticmethod
    def _flow(snapshot: CatalogSnapshot, flow: Flow) -> FlowView:
        columns: list[ColumnLinkView] = []
        for link in flow.columns:
            columns.append(
                ColumnLinkView(from_column=link.from_column, to_column=link.to_column)
            )

        return FlowView(
            id=flow.id,
            from_node=snapshot.nodes[flow.from_node_id].label,
            from_node_id=flow.from_node_id,
            to_node=snapshot.nodes[flow.to_node_id].label,
            to_node_id=flow.to_node_id,
            columns=tuple(columns),
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

    async def read(self, process: str, nodes: str) -> tuple[str, ToolResult]:
        try:
            subject = self._subject()
            service = await self._service()
            if not process.strip():
                listed = await service.list_processes(subject)
                return pack_result(self._processes_table(listed))

            found = await self._process_by(service, subject, process)
            snapshot = await service.snapshot(subject, found.id)
            pins = await service.published_pins(subject, found.id)
            resolver = await service.resolver_of(subject, pins)
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        labels = self._names(nodes)
        view = CatalogView.of(found, snapshot, pins, resolver, labels)

        return pack_result(JsonResult(payload=view.model_dump(mode="json")))

    async def draft(self, process: str, name: str) -> tuple[str, ToolResult]:
        """Черновик процесса либо нового процесса (пустой process); пустое
        имя — свои открытые черновики."""
        try:
            subject = self._subject()
            service = await self._service()
            found = None
            if process.strip():
                found = await self._process_by(service, subject, process)

            if not name.strip():
                drafts = await service.my_drafts(subject)
                return pack_result(self._drafts_table(drafts, found))

            process_id = None
            if found is not None:
                process_id = found.id

            created = await service.create_draft(subject, process_id, name.strip())
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        if found is None:
            text = (
                f"draft created: {created.id} ({created.name!r}) of a new process "
                "that gets this name when published; propose operations with "
                "catalog_propose"
            )
        else:
            text = (
                f"draft created: {created.id} ({created.name!r}) over version "
                f"{created.base_version} of process {found.name!r}; propose "
                "operations with catalog_propose"
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

    async def sync(self, connection: str, schemas: str) -> tuple[str, ToolResult]:
        """Синхронизация подключения до конца: запись версии или причина отказа."""
        try:
            context = CallContext.current()
            service = await self._service()
            info = await self._connection_by(service, context.subject, connection)
            caller = SyncCaller(
                subject=context.subject,
                initiator=context.initiator,
                credential=context.credential,
            )
            scope = SyncScope(schemas=self._schemas(schemas))
            started = await service.start_sync(caller, info.id, scope)
            finished = await service.syncs.wait(started.id)
        except (RefusalError, CatalogServiceError) as exc:
            return pack_result(self._error(exc))

        payload = finished.model_dump(mode="json")
        if finished.status is SyncStatus.DONE:
            return pack_result(JsonResult(payload=payload))

        message = (
            f"sync {finished.id} of connection {info.name!r} ended as "
            f"{finished.status.value}: {finished.error}"
        )
        return pack_result(
            ErrorResult(message=message, error_kind=CatalogToolError.SYNC_REFUSED)
        )

    async def _process_by(
        self, service: CatalogService, subject: Subject, raw: str
    ) -> Process:
        """Процесс по имени либо по id.

        Ошибки:
        ProcessNotFoundError — ни по имени, ни по id.
        """
        for process in await service.list_processes(subject):
            if process.name == raw.strip():
                return process

        return await service.process(subject, self._uuid(raw))

    async def _connection_by(
        self, service: CatalogService, subject: Subject, raw: str
    ) -> ConnectionInfo:
        """Подключение по имени либо по id глазами субъекта.

        Ошибки:
        SyncSetupError — подключение субъекту не видно.
        """
        directory = service.syncs.directory
        try:
            return await directory.named(subject, raw.strip())
        except SyncSetupError:
            return await directory.info_of(subject, self._uuid(raw))

    @staticmethod
    def _schemas(raw: str) -> tuple[str, ...]:
        names: list[str] = []
        for piece in raw.split(","):
            name = piece.strip()
            if name:
                names.append(name)

        return tuple(names)

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

        process = await service.process(subject, entity_id)
        return process.name, CatalogPageUrl.process(prefix, entity_id)

    @staticmethod
    def _subject() -> Subject:
        """Субъект текущего вызова; вне хода — RefusalError."""
        return CallContext.current_subject()

    @staticmethod
    def _uuid(raw: str) -> UUID:
        try:
            return UUID(raw.strip())
        except ValueError as exc:
            msg = f"expected a uuid id, got {raw!r}: {exc}"
            raise RefusalError(CatalogToolError.BAD_ID.value, msg) from exc

    @staticmethod
    def _link_kind(raw: str) -> CatalogLinkKind:
        try:
            return CatalogLinkKind(raw.strip().lower())
        except ValueError as exc:
            msg = f"kind must be 'process' or 'draft', got {raw!r}"
            raise RefusalError(CatalogToolError.BAD_ID.value, msg) from exc

    @staticmethod
    def _operations(raw: str) -> OperationList:
        try:
            return OperationList.model_validate_json(raw)
        except ValidationError as exc:
            details = ValidationText.of(exc)
            msg = f"operations json {raw[:200]!r} does not parse: {details}"
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
    def _processes_table(processes: Sequence[Process]) -> ToolResult:
        rows: list[dict[str, Any]] = []
        for process in processes:
            rows.append(
                {
                    "process_id": str(process.id),
                    "name": process.name,
                    "description": process.description,
                    "version": process.latest_version,
                    "nodes": process.nodes,
                    "open_drafts": process.open_drafts,
                }
            )

        if not rows:
            return TextResult(
                text="no processes yet; the user creates one on the catalog page"
            )

        return TableResult(rows=rows)

    @staticmethod
    def _drafts_table(drafts: Sequence[Draft], process: Process | None) -> ToolResult:
        """Свои открытые черновики; с процессом — только его."""
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            if process is not None and draft.process_id != process.id:
                continue

            process_id = ""
            if draft.process_id is not None:
                process_id = str(draft.process_id)

            rows.append(
                {
                    "draft_id": str(draft.id),
                    "name": draft.name,
                    "process_id": process_id,
                    "base_version": draft.base_version,
                    "created_at": draft.created_at.isoformat(timespec="seconds"),
                }
            )

        if not rows:
            return TextResult(text="no open drafts; create one with catalog_draft")

        return TableResult(rows=rows)

    ERROR_KINDS: ClassVar[tuple[tuple[type[Exception], CatalogToolError], ...]] = (
        (DraftNotFoundError, CatalogToolError.NOT_FOUND),
        (ProcessNotFoundError, CatalogToolError.NOT_FOUND),
        (DraftClosedError, CatalogToolError.DRAFT_CLOSED),
        (DraftStaleError, CatalogToolError.DRAFT_STALE),
        (SyncRunningError, CatalogToolError.SYNC_REFUSED),
        (SyncSetupError, CatalogToolError.SYNC_REFUSED),
        (SnapshotKindMismatchError, CatalogToolError.SYNC_REFUSED),
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

        logger.error("catalog tool: store failure: %s", exc)
        message = f"catalog store failure: {exc}"
        return ErrorResult(message=message, error_kind=CatalogToolError.STORE)


def build_catalog_tools(
    cfg: CatalogToolConfig, service: ServiceSource, prefix: PrefixSource
) -> list[BaseTool]:
    tools = CatalogTools(service, prefix)
    ToolCallViews.register("catalog_propose", ScriptCall(arg="operations", lang="json"))

    @tool(response_format="content_and_artifact")
    async def catalog_read(
        process: Annotated[str, Field(description=CatalogPrompt.PROCESS)],
        nodes: Annotated[str, Field(description=CatalogPrompt.NODES)],
    ) -> tuple[str, ToolResult]:
        """List the data flow processes (empty process) or read a published
        process: nodes (objects of synced connections with their columns,
        canvas positions and optional groups), groups and flows between nodes
        with their column pairs. Call it before proposing changes to learn the
        existing ids and addresses."""
        return await tools.read(process, nodes)

    @tool(response_format="content_and_artifact")
    async def catalog_draft(
        process: Annotated[str, Field(description=CatalogPrompt.DRAFT_PROCESS)],
        name: Annotated[str, Field(description=CatalogPrompt.DRAFT_NAME)],
    ) -> tuple[str, ToolResult]:
        """Create a draft of a process (or of a new process when the process is
        empty) or list the user's open drafts (empty name). Changes go into a
        draft first; the user reviews and publishes it on the catalog page."""
        return await tools.draft(process, name)

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
        """Put a link to the catalog page of a process or a draft into the
        chat so the user can open the diagram."""
        return await tools.open(kind, entity_id)

    @tool(response_format="content_and_artifact")
    async def catalog_sync(
        connection: Annotated[
            str, Field(min_length=1, description=CatalogPrompt.SYNC_CONNECTION)
        ],
        schemas: Annotated[str, Field(description=CatalogPrompt.SYNC_SCHEMAS)],
    ) -> tuple[str, ToolResult]:
        """Snapshot the structure of a database behind a connection and store
        it as a new catalog version of that connection. Waits for the sync to
        finish and returns its record: version number, object counts or the
        failure reason."""
        return await tools.sync(connection, schemas)

    return [
        catalog_read,
        catalog_draft,
        catalog_propose,
        catalog_diff,
        catalog_open,
        catalog_sync,
    ]
