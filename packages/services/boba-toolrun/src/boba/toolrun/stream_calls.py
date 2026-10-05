"""Вызовы инструментов модели через DAG и порт сервера инструментов.

Каждый вызов инструмента исполняет DAG. Инструмент без портов — DAG из
одного узла. Потоковые инструменты (с портами каналов) модель связывает
именами каналов: выходной порт — поле `out="orders"`, входной — поле
`feed="orders"` или список `feeds=["a", "b"]`. Связать их можно двумя
способами: несколькими вызовами в одном ответе модели — тогда потоковые
вызовы ответа идут одним DAG, — либо одним вызовом инструмента workflow,
узлы которого называют инструменты и их аргументы. Здесь части этого пути:

- StreamChannelFields заменяет порты в схеме инструмента полями каналов,
  барьер группы StreamGroup из схемы убирает.
- WorkflowTool — инструмент workflow для модели: узел называет инструмент из
  списка сервера и его аргументы; схем инструментов в себе не несёт.
- CallDag строит описание DAG (boba.toolkit.dag): одного вызова, вызова
  workflow и потоковых вызовов одного ответа.
- ToolServer — порт сервера инструментов для клиента (чата): инструменты
  для модели, объявленные возможности и вызовы. LocalDagService — его
  реализация исполнителем в своём процессе; ToolServers — несколько
  серверов за одним портом с маршрутом по имени инструмента.
- NodeArgs — правило аргументов узла, которое исполнитель применяет до
  старта DAG.

Ошибки:
StreamGroupsConfigError — секции [stream_groups] нет в конфиге.
ToolServersError — два сервера за одним портом отдают инструмент с одним
    именем.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Protocol

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from boba.toolkit.chain import (
    CallAmbient,
    StreamFailureKind,
    StreamPlanError,
    StreamTimings,
)
from boba.toolkit.dag import (
    DagNode,
    DagPlanner,
    DagSpec,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.entry import EntryErrorKind, ToolArgv
from boba.toolkit.failure import ToolRefusalError, ValidationText
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpecs
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    ToolResultBase,
)
from boba.toolrun.call_id import CallFields
from boba.toolrun.dag_run import DagOutcome, DagRunError, DagRunner, NodeOutcome
from boba.toolrun.wrapping import ToolSchema

__all__ = [
    "CallDag",
    "CallReply",
    "LocalDagService",
    "NodeArgs",
    "StreamChannelFields",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
    "ToolServer",
    "ToolServers",
    "ToolServersError",
    "WorkflowTool",
]

logger = logging.getLogger(__name__)


class StreamGroupsConfigError(RuntimeError):
    """Секции сроков групп потоковых вызовов нет в конфиге."""


class ToolServersError(RuntimeError):
    """Серверы за одним портом нельзя собрать: имена инструментов совпали."""


class StreamGroupsConfig(BaseModel):
    """Секция конфига [stream_groups]: сроки группы связанных вызовов и
    размер буфера пайпов каналов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "stream_groups"

    PIPE_BYTES_FLOOR: ClassVar[int] = 4096

    open_sec: float = Field(
        gt=0, description="Сколько ждать, пока откроются все вызовы группы."
    )
    stall_sec: float = Field(
        gt=0,
        description="Сколько терпеть отсутствие движения данных по открытым каналам.",
    )
    poll_sec: float = Field(gt=0, description="Шаг проверки сроков группы.")
    pipe_bytes: int = Field(
        ge=PIPE_BYTES_FLOOR,
        description="Буфер пайпа канала, когда вызов его не назвал.",
    )
    pipe_bytes_max: int = Field(
        ge=PIPE_BYTES_FLOOR,
        description="Потолок буфера, который можно запросить полем вызова.",
    )

    @model_validator(mode="after")
    def _max_covers_default(self) -> StreamGroupsConfig:
        if self.pipe_bytes > self.pipe_bytes_max:
            msg = (
                f"[{self.SECTION}]: pipe_bytes {self.pipe_bytes} exceeds "
                f"pipe_bytes_max {self.pipe_bytes_max}"
            )
            raise ValueError(msg)

        return self

    def timings(self) -> StreamTimings:
        return StreamTimings(
            open_sec=self.open_sec, stall_sec=self.stall_sec, poll_sec=self.poll_sec
        )


class StreamFieldText(StrEnum):
    """Описания полей каналов для модели."""

    OUTBOUND = (
        "Имя канала, в который вызов пишет свой поток:\n"
        "   - любое короткое имя, например orders\n"
        "   - читатели называют это же имя в своих входах\n"
        "   - у канала один писатель, читателей сколько угодно\n"
        "       каждый читатель получает поток целиком\n"
        "   - писатель и все его читатели — узлы одного вызова workflow\n"
    )
    OUTBOUND_MANY = (
        "Имена каналов, которые вызов пишет, списком:\n"
        "   - каждый канал — отдельный поток со своими данными\n"
        "   - у каждого имени свои читатели в этом же вызове workflow\n"
        "   - у канала один писатель, читателей сколько угодно\n"
    )
    INBOUND = (
        "Имя канала, из которого вызов читает поток:\n"
        "   - имя выхода другого узла этого же вызова workflow\n"
        "   - писатель и читатели — узлы одного вызова workflow\n"
    )
    INBOUND_MANY = (
        "Имена каналов, которые вызов читает, списком:\n"
        "   - каждое имя — выход другого узла этого же вызова workflow\n"
        "   - каждый канал приходит отдельным потоком, данные не смешиваются\n"
    )

    @classmethod
    def of(cls, port: PortDecl) -> StreamFieldText:
        if port.direction is PortDirection.OUTBOUND:
            if port.many:
                return cls.OUTBOUND_MANY

            return cls.OUTBOUND

        if port.many:
            return cls.INBOUND_MANY

        return cls.INBOUND


class StreamChannelFields:
    """Поля каналов в схеме модели на месте портов потокового инструмента.

    Порт — канал вызова, значение ему строит гость; модели же нужно назвать
    канал. Поле получает имя порта: одиночный порт — строка с именем канала,
    порт-список — список имён. Писатель каналов получает ещё поле
    pipe_bytes — буфер пайпов своих каналов, с дефолтом и потолком из
    секции [stream_groups]. Барьер группы StreamGroup модели не виден.
    Обёртка запуска поля каналов не сериализует: её схема захвачена до
    пересборки, и этих имён в ней нет.
    """

    PIPE_TEXT: ClassVar[str] = (
        "Буфер пайпа каждого канала этого вызова, в байтах:\n"
        "   - крупнее — быстрее массивная перекачка\n"
        "   - мельче — экономнее общий бюджет пайпов ядра\n"
        "   - действует на выходы вызова и входы их читателей\n"
        "   - ядро не дало запрошенное — группа не запускается\n"
    )

    def __init__(self, config: StreamGroupsConfig) -> None:
        self._config = config

    def attach_all(self, tools: Sequence[BaseTool], specs: StreamSpecs) -> None:
        for tool in tools:
            self._attach(tool, specs)

    def _attach(self, tool: BaseTool, specs: StreamSpecs) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        spec = specs.of(tool.name)
        groups = ToolArgv.group_fields(schema)
        if not spec.streaming() and not groups:
            return

        fields: dict[str, tuple[Any, Any]] = {}
        drop: list[str] = list(groups)
        for port in spec.ports:
            drop.append(port.name)
            fields[port.name] = self._field(port)

        if spec.outbound():
            fields[DagPlanner.PIPE_FIELD] = self._pipe_field()

        tool.args_schema = ToolSchema.rebuild(schema, fields, drop)

    def _pipe_field(self) -> tuple[Any, Any]:
        declared = Annotated[
            int,
            Field(
                ge=StreamGroupsConfig.PIPE_BYTES_FLOOR,
                le=self._config.pipe_bytes_max,
                description=self.PIPE_TEXT,
            ),
        ]
        return (declared, self._config.pipe_bytes)

    @staticmethod
    def _field(port: PortDecl) -> tuple[Any, Any]:
        text = StreamFieldText.of(port).value

        if port.many:
            return (Annotated[list[str], Field(min_length=1, description=text)], ...)

        return (Annotated[str, Field(min_length=1, description=text)], ...)


class WorkflowNodeCall(BaseModel):
    """Узел вызова workflow, как его описывает модель: имя узла, инструмент
    из списка сервера и его аргументы."""

    key: Annotated[
        str,
        Field(min_length=1, description="Короткое уникальное имя узла в этом вызове."),
    ]
    tool: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя инструмента из списка инструментов, кроме workflow.",
        ),
    ]
    args: Annotated[
        dict[str, Any],
        Field(description="Аргументы инструмента tool — те же, что при его вызове."),
    ]


class WorkflowCall(BaseModel):
    """Аргументы вызова workflow: узлы связки."""

    nodes: Annotated[
        list[WorkflowNodeCall],
        Field(min_length=1, description="Узлы связки: вызовы инструментов."),
    ]


class WorkflowTool:
    """Инструмент workflow для модели: связка инструментов одним вызовом.

    Потоковому инструменту нужен партнёр на другом конце канала. Модель
    может позвать партнёров отдельными вызовами в одном ответе либо описать
    связку узлами одного вызова workflow. Узел называет инструмент из
    списка сервера и его аргументы; схем инструментов workflow не несёт —
    они у самих инструментов. Создаёт его LocalDagService; исполняет вызов
    не тело инструмента, а DAG — тело существует, чтобы инструмент был
    обычным инструментом графа.
    """

    NAME: ClassVar[str] = "workflow"

    FEATURE: ClassVar[str] = "com.boba/workflow"
    """Возможность сервера: каким инструментом связка описывается одним
    вызовом; узлы этого вызова — вызовы других инструментов сервера."""

    DESCRIPTION: ClassVar[str] = (
        "Запустить несколько инструментов одной связкой:\n"
        "   - узел — вызов инструмента из списка инструментов: tool и его args\n"
        "       args — те же аргументы, что при обычном вызове этого инструмента\n"
        "   - потоковые инструменты связываются именами каналов в args\n"
        "       выход узла называет канал, вход другого узла называет его же\n"
        "   - у канала один писатель, читателей сколько угодно\n"
        "   - у каждого канала есть и писатель, и читатель среди узлов вызова\n"
        "   - узлы, связанные каналами, — группа «все или никто»\n"
        "       сбой любого узла группы отменяет результат всей группы\n"
        "   - несвязанные группы одного вызова идут одновременно и независимо\n"
        "   - имена каналов действуют внутри одного вызова workflow\n"
    )

    def __init__(self) -> None:
        self._fields = CallFields()

    def build(self) -> BaseTool:
        """Инструмент для модели: узлы называют инструменты сервера по имени."""
        built = StructuredTool.from_function(
            coroutine=self._never_called,
            name=self.NAME,
            description=self.DESCRIPTION,
            args_schema=WorkflowCall,
        )
        self._fields.attach_all([built])

        return built

    TOOL: ClassVar[str] = "tool"
    """Ключ настроек возможности: имя инструмента-связки."""

    LINKED: ClassVar[str] = "linked"
    """Ключ настроек возможности: имена потоковых инструментов. Их вызовы
    из одного ответа модели связывают имена каналов, поэтому клиент шлёт
    такие вызовы одной связкой."""

    def feature(self, linked: frozenset[str]) -> Mapping[str, object]:
        return {self.TOOL: self.NAME, self.LINKED: sorted(linked)}

    @classmethod
    async def _never_called(cls, **kwargs: object) -> str:
        msg = (
            f"tool {cls.NAME!r} is executed by the dag runner through the agent "
            "middleware; its body must not be called"
        )
        raise RuntimeError(msg)


class CallDag:
    """Описание DAG по вызовам модели.

    Обычный вызов — DAG из одного узла с ключом tool_call_id. Потоковые
    вызовы одного ответа модели — один DAG: узел на вызов, ключ узла —
    tool_call_id вызова. Узлы вызова workflow — уже узлы описания DAG
    (DagSpec); ключ узла заменяется на идентификатор вызова узла (под ним
    идут журнал и шаг ленты узла): заданный клиентом call_id, иначе
    tool_call_id вызова с номером узла. Имя, данное узлу моделью, едет в
    title — им узел называется в текстах отказов.
    """

    NODES: ClassVar[str] = "nodes"

    def of(self, call: ToolCall) -> DagSpec:
        """DAG одного вызова.

        Ошибки:
        StreamPlanError — вызов без tool_call_id; вызов workflow не проходит
            форму описания DAG.
        """
        call_id = self.id_of(call)

        if call["name"] != WorkflowTool.NAME:
            return DagSpec(name=call_id, version=1, nodes=[self._node(call)])

        described = self._described(call_id, call)

        return DagSpec(
            name=call_id, version=1, nodes=list(self._nodes(call_id, described))
        )

    def of_linked(self, calls: Sequence[ToolCall]) -> DagSpec:
        """DAG потоковых вызовов одного ответа: их связывают имена каналов.

        Ошибки:
        StreamPlanError — у вызова нет tool_call_id.
        """
        nodes: list[DagNode] = []
        for call in calls:
            nodes.append(self._node(call))

        return DagSpec(name=nodes[0].key, version=1, nodes=nodes)

    def _node(self, call: ToolCall) -> DagNode:
        return DagNode(key=self.id_of(call), tool=call["name"], args=dict(call["args"]))

    def _described(self, call_id: str, call: ToolCall) -> DagSpec:
        """Узлы вызова workflow, как их назвала модель."""
        raw: dict[str, object] = {"name": call_id, "version": 1}
        if self.NODES in call["args"]:
            raw[self.NODES] = call["args"][self.NODES]

        try:
            return DagSpec.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"workflow call {call_id!r} does not match its schema: "
                f"{ValidationText.of(exc)}"
            )
            raise StreamPlanError(msg) from exc

    def _nodes(self, call_id: str, described: DagSpec) -> Iterator[DagNode]:
        for index, node in enumerate(described.nodes):
            key = node.call_id
            if not key:
                key = f"{call_id}_{index}"

            yield node.model_copy(update={"key": key, "title": node.key})

    @staticmethod
    def id_of(call: ToolCall) -> str:
        key = call["id"]
        if not key:
            name = call["name"]
            msg = f"dag plan: call of {name!r} has no tool call id"
            raise StreamPlanError(msg)

        return key


class ToolServer(Protocol):
    """Порт сервера инструментов для клиента.

    Клиент (чат) знает исполнение только через него: берёт инструменты,
    которые сервер отдаёт модели, читает объявленные сервером возможности и
    шлёт вызовы. Что стоит за портом, клиенту неизвестно: исполнитель своего
    процесса (LocalDagService), несколько серверов с маршрутом по имени
    (ToolServers) либо обёртка клиента над другим портом.
    """

    @abstractmethod
    def tools(self) -> Sequence[BaseTool]:
        """Инструменты, которые сервер отдаёт модели."""
        ...

    @abstractmethod
    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Возможности, которые сервер объявляет клиенту при подключении:
        идентификатор возможности → её настройки."""
        ...

    @abstractmethod
    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        """Принять вызовы одного ответа модели; итог каждого — своё ожидание.

        Ожидания идут в порядке вызовов и кончаются независимо: быстрый
        вызов отвечает, не дожидаясь медленного соседа. Вызовы пакета сервер
        вправе связать между собой: потоковые вызовы одного ответа связывают
        имена каналов. Отмена ожидания останавливает его вызов.
        """
        ...

    async def call(self, call: ToolCall) -> ToolMessage:
        """Исполнить один вызов и дождаться его итога."""
        pending = await self.submit([call])

        return await pending[0]


class NodeArgs(Protocol):
    """Правило аргументов узла, которое исполнитель применяет вне тела.

    Обвязки тела узнают о негодном аргументе, когда узел уже запущен, а
    соседние группы вызова идут. Правило проверяет аргументы каждого узла до
    старта DAG, решает, какими аргументы узла вернутся клиенту в итоге
    вызова, и объявляет клиенту возможность сервера, без которой такой
    аргумент не собрать. Реализует его владелец вида аргумента (запечатанные
    соединения — SealedConnectionParams); исполнитель про вид не знает.
    """

    @abstractmethod
    async def check(self, tool: str, args: Mapping[str, object]) -> None:
        """Отказ ToolRefusalError, если аргументы узла инструмента tool негодны."""
        ...

    @abstractmethod
    def shown(self, tool: str, args: Mapping[str, object]) -> Mapping[str, object]:
        """Аргументы узла для итога вызова: без значений, которые клиенту
        возвращать незачем."""
        ...

    @abstractmethod
    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Возможности сервера, которые правило объявляет клиенту."""
        ...


class LocalDagService(ToolServer):
    """Реализация порта ToolServer исполнителем в своём процессе.

    Клиент получает отсюда инструменты для модели и шлёт вызовы. Модели
    отдаются все инструменты как есть; если среди них есть потоковые, к ним
    добавляется workflow. Каждый вызов исполняет DAG: обычный вызов и вызов
    workflow — свой, потоковые вызовы одного пакета — общий, их связывают
    имена каналов. Отказ плана и отказ правила аргументов (NodeArgs) —
    итог-ошибка вызова до старта узлов; у общего DAG её получает каждый его
    вызов. Выдуманное имя получает отказ со списком инструментов. Создаётся
    из обёрнутых инструментов реестра, секции [stream_groups], правил
    аргументов и потоковых деклараций инструментов.
    """

    def __init__(
        self,
        tools: Sequence[BaseTool],
        config: StreamGroupsConfig,
        rules: Sequence[NodeArgs],
        specs: StreamSpecs,
        ambient: CallAmbient,
    ) -> None:
        self._rules = tuple(rules)

        by_name: dict[str, BaseTool] = {}
        linked: list[str] = []
        for tool in tools:
            by_name[tool.name] = tool
            if specs.of(tool.name).streaming():
                linked.append(tool.name)

        offered = list(tools)
        self._workflow: WorkflowTool | None = None
        if linked:
            self._workflow = WorkflowTool()
            offered.append(self._workflow.build())

        self._offered = offered
        self._offered_names = self._names_of(offered)
        self._unknown = UnknownTool(self._offered_names)
        self._linked_names = frozenset(linked)
        self._dags = CallDag()
        self._runner = DagRunner(
            by_name, specs, ambient, config.timings(), config.pipe_bytes
        )

    def tools(self) -> Sequence[BaseTool]:
        return self._offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        declared: dict[str, Mapping[str, object]] = {}
        for rule in self._rules:
            declared.update(rule.features())

        if self._workflow is not None:
            declared[WorkflowTool.FEATURE] = self._workflow.feature(self._linked_names)

        return declared

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        linked: list[ToolCall] = []
        for call in calls:
            if call["name"] in self._linked_names:
                linked.append(call)

        shared = asyncio.ensure_future(self._linked_run(linked))

        pending: list[asyncio.Future[ToolMessage]] = []
        for call in calls:
            if call["name"] in self._linked_names:
                pending.append(asyncio.ensure_future(self._picked(shared, call)))
                continue

            pending.append(asyncio.ensure_future(self._single(call)))

        return pending

    async def _single(self, call: ToolCall) -> ToolMessage:
        """Итог вызова, который идёт своим DAG: обычный инструмент или workflow."""
        name = call["name"]
        if name not in self._offered_names:
            return self._refused(call, self._unknown.refusal(name))

        try:
            dag = self._dags.of(call)
        except StreamPlanError as exc:
            return self._refused(call, self._plan_refusal(exc))

        outcome = await self._outcome(dag)
        if isinstance(outcome, FailureResult):
            return self._refused(call, outcome)

        if name != WorkflowTool.NAME:
            return outcome.nodes[0].message(dag.nodes[0].key)

        nodes: list[WorkflowNodeResult] = []
        for node, ended in zip(dag.nodes, outcome.nodes, strict=True):
            nodes.append(self._node_result(node, ended))

        failed = not outcome.ok()

        result = WorkflowResult(nodes=nodes, ok=not failed)

        return CallReply(call).message(result, failed)

    async def _linked_run(
        self, calls: Sequence[ToolCall]
    ) -> DagOutcome | FailureResult | None:
        """Общий DAG потоковых вызовов пакета: его итог либо отказ до старта."""
        if not calls:
            return None

        try:
            dag = self._dags.of_linked(calls)
        except StreamPlanError as exc:
            return self._plan_refusal(exc)

        return await self._outcome(dag)

    async def _picked(
        self,
        shared: asyncio.Future[DagOutcome | FailureResult | None],
        call: ToolCall,
    ) -> ToolMessage:
        """Итог одного потокового вызова из итога общего DAG; отмена вызова
        останавливает общий DAG — его группа живёт по правилу «все или никто»."""
        try:
            outcome = await shared
        except asyncio.CancelledError:
            shared.cancel()
            raise

        if isinstance(outcome, DagOutcome):
            key = self._dags.id_of(call)

            return outcome.node(key).message(key)

        if outcome is None:
            msg = f"linked call of {call['name']!r} has no dag outcome"
            raise DagRunError(msg)

        return self._refused(call, outcome)

    async def _outcome(self, dag: DagSpec) -> DagOutcome | FailureResult:
        """Итог DAG; отказ плана или правила аргументов — отказ до старта."""
        try:
            await self._check_args(dag)
            handle = self._runner.start(dag)
        except (StreamPlanError, DagRunError) as exc:
            return self._plan_refusal(exc)
        except ToolRefusalError as exc:
            logger.warning("dag arguments refused: %s", exc)
            return exc.failure()

        try:
            return await handle.outcome()
        except asyncio.CancelledError:
            handle.cancel()
            raise

    @staticmethod
    def _plan_refusal(exc: Exception) -> ErrorResult:
        logger.warning("dag plan refused: %s", exc)

        return ErrorResult(message=str(exc), error_kind=StreamFailureKind.PLAN_REFUSED)

    async def _check_args(self, dag: DagSpec) -> None:
        """Правила аргументов по каждому узлу; отказ узла workflow называет узел."""
        for node in dag.nodes:
            for rule in self._rules:
                await self._check_node(rule, node)

    @staticmethod
    async def _check_node(rule: NodeArgs, node: DagNode) -> None:
        try:
            await rule.check(node.tool, node.args)
        except ToolRefusalError as exc:
            if not node.title:
                raise

            msg = f"node {node.title!r}: {exc}"
            raise ToolRefusalError(exc.kind, msg) from exc

    def _shown_args(self, node: DagNode) -> Mapping[str, object]:
        shown: Mapping[str, object] = dict(node.args)
        for rule in self._rules:
            shown = rule.shown(node.tool, shown)

        return shown

    def _node_result(self, node: DagNode, outcome: NodeOutcome) -> WorkflowNodeResult:
        return WorkflowNodeResult(
            key=node.title,
            call_id=node.key,
            tool=node.tool,
            args=self._shown_args(node),
            errored=outcome.errored,
            content=outcome.content,
            result=outcome.artifact,
        )

    def _refused(self, call: ToolCall, failure: FailureResult) -> ToolMessage:
        return CallReply(call).message(failure, True)

    @staticmethod
    def _names_of(tools: Sequence[BaseTool]) -> frozenset[str]:
        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        return frozenset(names)


class UnknownTool:
    """Отказ вызову с именем, которого у сервера нет: называет, какие есть."""

    def __init__(self, known: frozenset[str]) -> None:
        self._known = known

    def refusal(self, name: str) -> ErrorResult:
        msg = (
            f"tool {name!r} does not exist; the available tools are "
            f"{sorted(self._known)}"
        )

        return ErrorResult(message=msg, error_kind=EntryErrorKind.UNKNOWN_TOOL)


class CallReply:
    """Ответ на вызов, собранный самим сервером, а не узлом DAG: отказ до
    старта и общий итог workflow. Сообщение то же, что у узла."""

    def __init__(self, call: ToolCall) -> None:
        self._call = call

    def message(self, result: ToolResultBase, errored: bool) -> ToolMessage:
        content, artifact = result.packed()
        call_id = str(self._call["id"])
        outcome = NodeOutcome(
            key=call_id,
            tool=self._call["name"],
            content=content,
            artifact=artifact,
            errored=errored,
        )

        return outcome.message(call_id)

    def refused(self, failure: FailureResult) -> asyncio.Future[ToolMessage]:
        """Готовый отказ вызову, который до исполнения не дошёл."""
        done: asyncio.Future[ToolMessage] = asyncio.get_running_loop().create_future()
        done.set_result(self.message(failure, True))

        return done


class ToolServers(ToolServer):
    """Несколько серверов инструментов за одним портом.

    Клиент видит один порт: инструменты и возможности всех серверов вместе,
    вызов уходит тому серверу, чей это инструмент. Маршрут — имя
    инструмента; вызовы пакета к одному серверу идут ему одним пакетом.
    Выдуманное имя получает отказ со списком всех инструментов. Собирает
    его реестр инструментов (ToolRegistry.server).
    """

    def __init__(self, servers: Sequence[ToolServer]) -> None:
        self._servers = tuple(servers)
        self._owner: dict[str, int] = {}
        for index, server in enumerate(self._servers):
            for tool in server.tools():
                self._claim(tool.name, index)

        self._unknown = UnknownTool(frozenset(self._owner))

    def tools(self) -> Sequence[BaseTool]:
        offered: list[BaseTool] = []
        for server in self._servers:
            offered.extend(server.tools())

        return offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        declared: dict[str, Mapping[str, object]] = {}
        for server in self._servers:
            declared.update(server.features())

        return declared

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        routed: dict[int, list[int]] = {}
        pending: dict[int, asyncio.Future[ToolMessage]] = {}
        for position, call in enumerate(calls):
            owner = self._owner.get(call["name"])
            if owner is None:
                refusal = self._unknown.refusal(call["name"])
                pending[position] = CallReply(call).refused(refusal)
                continue

            routed.setdefault(owner, []).append(position)

        for owner, positions in routed.items():
            batch: list[ToolCall] = []
            for position in positions:
                batch.append(calls[position])

            accepted = await self._servers[owner].submit(batch)
            for position, future in zip(positions, accepted, strict=True):
                pending[position] = future

        ordered: list[asyncio.Future[ToolMessage]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    def _claim(self, name: str, index: int) -> None:
        if name in self._owner:
            msg = (
                f"tool servers: tool {name!r} is offered by two servers "
                f"(#{self._owner[name]} and #{index}); a tool name routes to "
                "exactly one server"
            )
            raise ToolServersError(msg)

        self._owner[name] = index
