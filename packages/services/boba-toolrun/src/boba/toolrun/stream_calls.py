"""Вызовы инструментов модели через DAG; связка насосов — workflow.

Каждый вызов инструмента модели исполняет свой DAG. Инструмент без портов
модель зовёт сам по себе — это DAG из одного узла. Насосы и трансформы
модель связывает одним вызовом инструмента workflow: его узлы — вызовы
потоковых инструментов, выходной порт узла — поле `out="orders"`, входной —
поле `feed="orders"` или список `feeds=["a", "b"]`. Имена каналов живут в
пределах одного вызова workflow. Здесь четыре части этого пути:

- StreamChannelFields заменяет порты в схеме инструмента полями каналов,
  барьер группы StreamGroup из схемы убирает.
- WorkflowTool — инструмент workflow для модели: его схема несёт схемы
  потоковых инструментов сессии, сами они модели отдельно не отдаются.
- CallDag строит описание DAG (boba.toolkit.dag) одного вызова модели:
  обычный вызов — узел с ключом tool_call_id, вызов workflow раскрывается в
  свои узлы.
- ToolServer — порт сервера инструментов для клиента (чата): инструменты
  для модели и вызов. LocalDagService — его реализация исполнителем в своём
  процессе: на вызов строит DAG, отдаёт его DagRunner и возвращает итог
  сообщением инструмента. Отказ плана — итог-ошибка этого вызова, соседние
  вызовы он не трогает.

Ошибки:
StreamGroupsConfigError — секции [stream_groups] нет в конфиге.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_validator,
)

from boba.toolkit.chain import StreamPlanError, StreamTimings
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec
from boba.toolkit.entry import EntryErrorKind, ToolArgv
from boba.toolkit.failure import ValidationText
from boba.toolkit.ports import PortDecl, PortDirection, ToolStreamSpecs
from boba.toolkit.result import (
    ChatView,
    ErrorResult,
    Fact,
    FactsBlock,
    FailureResult,
    ToolResult,
    ToolResultBase,
)
from boba.toolrun.dag_run import DagRunError, DagRunner, NodeOutcome
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.wrapping import ToolSchema

__all__ = [
    "CallDag",
    "LocalDagService",
    "StreamCallKind",
    "StreamChannelFields",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
    "ToolServer",
    "WorkflowNodeResult",
    "WorkflowResult",
    "WorkflowTool",
]

logger = logging.getLogger(__name__)


class StreamGroupsConfigError(RuntimeError):
    """Секции сроков групп потоковых вызовов нет в конфиге."""


class StreamCallKind(StrEnum):
    """Kind отказа потокового вызова до его запуска."""

    PLAN_REFUSED = "stream_plan_refused"
    OUTSIDE_WORKFLOW = "stream_tool_outside_workflow"


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

    def attach_all(self, tools: Sequence[BaseTool]) -> None:
        for tool in tools:
            self._attach(tool)

    def _attach(self, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        spec = ToolStreamSpecs.of(tool.name)
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


class WorkflowTool:
    """Инструмент workflow для модели: связка потоковых инструментов одним
    вызовом.

    Потоковый инструмент в одиночку не работает — ему нужен партнёр на
    другом конце канала. Поэтому модели такие инструменты отдельно не
    отдаются: она описывает всю связку узлами одного вызова workflow, и
    схема workflow несёт точные схемы потоковых инструментов сессии с
    выбором по имени инструмента. Создаёт его LocalDagService; исполняет вызов
    не тело инструмента, а DAG — тело существует, чтобы инструмент был
    обычным инструментом графа.
    """

    NAME: ClassVar[str] = "workflow"

    DESCRIPTION: ClassVar[str] = (
        "Запустить связку потоковых инструментов:\n"
        "   - узел — вызов одного инструмента: tool и его args\n"
        "   - узлы связываются именами каналов в args\n"
        "       выход узла называет канал, вход другого узла называет его же\n"
        "   - у канала один писатель, читателей сколько угодно\n"
        "   - у каждого канала есть и писатель, и читатель среди узлов вызова\n"
        "   - узлы, связанные каналами, — группа «все или никто»\n"
        "       сбой любого узла группы отменяет результат всей группы\n"
        "   - несвязанные группы одного вызова идут одновременно и независимо\n"
        "   - имена каналов действуют внутри одного вызова workflow\n"
    )

    KEY_TEXT: ClassVar[str] = "Короткое уникальное имя узла в этом вызове."
    NODES_TEXT: ClassVar[str] = "Узлы связки: вызовы потоковых инструментов."

    def __init__(self, pumps: Sequence[BaseTool]) -> None:
        self._pumps = list(pumps)

    def build(self) -> BaseTool:
        """Инструмент для модели со схемой узлов по инструментам сессии."""
        node: Any = self._node_model(self._pumps[0])
        for pump in self._pumps[1:]:
            node = node | self._node_model(pump)

        nodes: Any = Annotated[
            list[node], Field(min_length=1, description=self.NODES_TEXT)
        ]
        schema = create_model("WorkflowArgs", nodes=(nodes, ...))

        built = StructuredTool.from_function(
            coroutine=self._never_called,
            name=self.NAME,
            description=self.DESCRIPTION,
            args_schema=schema,
        )
        ToolIntentField.attach_all([built])

        return built

    def _node_model(self, pump: BaseTool) -> type[BaseModel]:
        """Модель узла инструмента pump: имя узла, имя инструмента и его
        аргументы по схеме, которую видит модель; описание инструмента едет
        в схеме его аргументов."""
        key: Any = Annotated[str, Field(min_length=1, description=self.KEY_TEXT)]
        tool: Any = Literal[pump.name]

        return create_model(
            f"WorkflowNode_{pump.name}",
            key=(key, ...),
            tool=(tool, ...),
            args=(pump.tool_call_schema, ...),
        )

    @classmethod
    async def _never_called(cls, **kwargs: object) -> str:
        msg = (
            f"tool {cls.NAME!r} is executed by the dag runner through the agent "
            "middleware; its body must not be called"
        )
        raise RuntimeError(msg)


class WorkflowNodeResult(BaseModel):
    """Итог узла workflow: какой инструмент с чем вызван и чем он кончился.

    key — имя узла, данное моделью; call_id — идентификатор вызова узла, под
    которым идут его журнал и шаг ленты. errored — вызов кончился ошибкой
    самого вызова (аргументы, права), а не результатом инструмента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    call_id: str
    tool: str
    args: Mapping[str, Any]
    errored: bool
    content: str
    result: ToolResult


class WorkflowResult(ToolResultBase):
    """Итог вызова workflow: результаты его узлов в порядке описания.

    Модель описывает связку потоковых инструментов одним вызовом workflow;
    исполнитель DAG отдаёт итог каждого узла, а этот результат несёт их
    модели и истории одним конвертом. Лента раскрывает его в шаги узлов —
    так же, как рисует их вживую.
    """

    kind: Literal["workflow"] = "workflow"
    nodes: Sequence[WorkflowNodeResult]

    def llm_view(self) -> str:
        parts: list[str] = []
        for node in self.nodes:
            parts.append(f"[{node.key}] {node.tool}:\n{node.content}")

        return "\n\n".join(parts)

    def chat_view(self) -> ChatView:
        return ChatView(markdown=FactsBlock(facts=self._facts()).markdown())

    def _facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for node in self.nodes:
            status = "ok"
            if not node.result.ok:
                status = "failed"

            facts.append(Fact(key=f"{node.key} ({node.tool})", value=status))

        return facts


class CallDag:
    """Описание DAG одного вызова модели.

    Обычный вызов — DAG из одного узла с ключом tool_call_id и аргументами,
    как назвала модель. Узлы вызова workflow — уже узлы описания DAG
    (DagSpec); ключ узла заменяется на tool_call_id вызова с номером узла
    (под ним идут журнал и шаг ленты узла), а имя, данное узлу моделью, едет
    в title — им узел называется в текстах отказов. Имя DAG — tool_call_id
    вызова.
    """

    NODES: ClassVar[str] = "nodes"

    def __init__(self, pumps: frozenset[str]) -> None:
        self._pumps = pumps

    def of(self, call: ToolCall) -> DagSpec:
        """DAG вызова.

        Ошибки:
        StreamPlanError — вызов без tool_call_id; вызов workflow не проходит
            форму описания DAG либо зовёт не потоковый инструмент.
        """
        call_id = self.id_of(call)

        if call["name"] != WorkflowTool.NAME:
            node = DagNode(key=call_id, tool=call["name"], args=dict(call["args"]))

            return DagSpec(name=call_id, version=1, nodes=[node])

        described = self._described(call_id, call)

        return DagSpec(
            name=call_id, version=1, nodes=list(self._nodes(call_id, described))
        )

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
            if node.tool not in self._pumps:
                msg = (
                    f"workflow call {call_id!r}: node {node.key!r} names tool "
                    f"{node.tool!r}, expected one of the stream tools "
                    f"{sorted(self._pumps)}; a tool without stream ports is "
                    "called directly, not through workflow"
                )
                raise StreamPlanError(msg)

            yield node.model_copy(
                update={"key": f"{call_id}_{index}", "title": node.key}
            )

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
    которые сервер отдаёт модели, и шлёт вызовы. Что стоит за портом,
    клиенту неизвестно; здесь это исполнитель своего процесса
    (LocalDagService).
    """

    @abstractmethod
    def tools(self) -> Sequence[BaseTool]:
        """Инструменты, которые сервер отдаёт модели."""
        ...

    @abstractmethod
    async def call(self, call: ToolCall) -> ToolMessage:
        """Исполнить вызов и дождаться его итога сообщением инструмента."""
        ...


class LocalDagService(ToolServer):
    """Реализация порта ToolServer исполнителем в своём процессе.

    Клиент (чат) получает отсюда инструменты для модели и шлёт вызовы;
    каждый вызов строит свой DAG и ждёт его итог: обычный вызов — итог
    своего узла, вызов workflow — итоги всех своих узлов одним результатом.
    Отказ плана — итог-ошибка этого вызова. Потоковый инструмент, вызванный
    мимо workflow, и выдуманное имя получают отказ с подсказкой. Создаётся
    из обёрнутых инструментов реестра и секции [stream_groups]; модели
    отдаёт инструменты без портов как есть, потоковые — одним инструментом
    workflow.
    """

    def __init__(self, tools: Sequence[BaseTool], config: StreamGroupsConfig) -> None:
        by_name: dict[str, BaseTool] = {}
        plain: list[BaseTool] = []
        pumps: list[BaseTool] = []
        for tool in tools:
            by_name[tool.name] = tool
            if ToolStreamSpecs.of(tool.name).streaming():
                pumps.append(tool)
                continue

            plain.append(tool)

        offered = list(plain)
        if pumps:
            offered.append(WorkflowTool(pumps).build())

        self._offered = offered
        self._offered_names = self._names_of(offered)
        self._pump_names = self._names_of(pumps)
        self._dags = CallDag(self._pump_names)
        self._runner = DagRunner(by_name, config.timings(), config.pipe_bytes)

    def tools(self) -> Sequence[BaseTool]:
        return self._offered

    async def call(self, call: ToolCall) -> ToolMessage:
        """Итог вызова call."""
        name = call["name"]
        if name in self._pump_names:
            return self._refused(call, self._outside_workflow(name))

        if name not in self._offered_names:
            return self._refused(call, self._unknown_tool(name))

        try:
            dag = self._dags.of(call)
            handle = self._runner.start(dag)
        except (StreamPlanError, DagRunError) as exc:
            logger.warning("dag plan refused: %s", exc)
            failure = ErrorResult(
                message=str(exc), error_kind=StreamCallKind.PLAN_REFUSED
            )
            return self._refused(call, failure)

        try:
            outcome = await handle.outcome()
        except asyncio.CancelledError:
            handle.cancel()
            raise

        if name != WorkflowTool.NAME:
            return outcome.nodes[0].message(dag.nodes[0].key)

        nodes: list[WorkflowNodeResult] = []
        for node, ended in zip(dag.nodes, outcome.nodes, strict=True):
            nodes.append(self._node_result(node, ended))

        failed = not outcome.ok()

        return self._message(call, WorkflowResult(nodes=nodes, ok=not failed), failed)

    @staticmethod
    def _node_result(node: DagNode, outcome: NodeOutcome) -> WorkflowNodeResult:
        return WorkflowNodeResult(
            key=node.title,
            call_id=node.key,
            tool=node.tool,
            args=dict(node.args),
            errored=outcome.errored,
            content=outcome.content,
            result=outcome.artifact,
        )

    @staticmethod
    def _outside_workflow(name: str) -> ErrorResult:
        msg = (
            f"tool {name!r} reads or writes stream channels and runs only as a "
            f"node of {WorkflowTool.NAME!r}: call {WorkflowTool.NAME!r} with a "
            f"node for {name!r} and nodes for the tools on the other ends of its "
            "channels"
        )

        return ErrorResult(message=msg, error_kind=StreamCallKind.OUTSIDE_WORKFLOW)

    def _unknown_tool(self, name: str) -> ErrorResult:
        msg = (
            f"tool {name!r} does not exist; the available tools are "
            f"{sorted(self._offered_names)}"
        )

        return ErrorResult(message=msg, error_kind=EntryErrorKind.UNKNOWN_TOOL)

    def _refused(self, call: ToolCall, failure: FailureResult) -> ToolMessage:
        return self._message(call, failure, True)

    def _message(
        self, call: ToolCall, result: ToolResultBase, errored: bool
    ) -> ToolMessage:
        """Результат вызова сообщением инструмента: тот же путь, что у узла."""
        content, artifact = result.packed()
        call_id = self._dags.id_of(call)
        outcome = NodeOutcome(
            key=call_id,
            tool=call["name"],
            content=content,
            artifact=artifact,
            errored=errored,
        )

        return outcome.message(call_id)

    @staticmethod
    def _names_of(tools: Sequence[BaseTool]) -> frozenset[str]:
        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        return frozenset(names)
