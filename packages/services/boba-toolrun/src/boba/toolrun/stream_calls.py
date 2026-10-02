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
- DagCalls — исполнение вызовов сессии: вызов строит свой DAG, отдаёт его
  исполнителю DagRunner и получает ToolMessage для модели. Отказ плана
  уходит модели сообщением-ошибкой этого вызова, соседние вызовы ответа
  он не трогает.

Ошибки:
StreamGroupsConfigError — секции [stream_groups] нет в конфиге.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    create_model,
    model_validator,
)

from boba.toolkit.chain import StreamPlanError, StreamTimings
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec
from boba.toolkit.entry import ToolArgv
from boba.toolkit.ports import PortDecl, PortDirection, ToolStreamSpecs
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolrun.dag_run import DagHandle, DagRunError, DagRunner, NodeOutcome
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.wrapping import ToolSchema

__all__ = [
    "CallDag",
    "DagCalls",
    "StreamCallKind",
    "StreamChannelFields",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
    "WorkflowCall",
    "WorkflowNode",
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


class WorkflowNode(BaseModel):
    """Узел вызова workflow, как его прислала модель: имя узла, инструмент
    и его аргументы с полями каналов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: Mapping[str, JsonValue] = {}


class WorkflowCall(BaseModel):
    """Аргументы вызова workflow: узлы связки, имена узлов уникальны.

    Форму проверяет эта модель; аргументы каждого узла проверяет сам
    инструмент узла при запуске, как у любого вызова. intent — подпись
    вызова, как у любого инструмента; шаги ленты рисуются по узлам, со
    своими подписями.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: Sequence[WorkflowNode] = Field(min_length=1)
    intent: str = ""

    @model_validator(mode="after")
    def _keys_are_unique(self) -> WorkflowCall:
        seen: set[str] = set()
        for node in self.nodes:
            if node.key in seen:
                msg = f"workflow node key {node.key!r} is repeated"
                raise ValueError(msg)

            seen.add(node.key)

        return self


class WorkflowTool:
    """Инструмент workflow для модели: связка потоковых инструментов одним
    вызовом.

    Потоковый инструмент в одиночку не работает — ему нужен партнёр на
    другом конце канала. Поэтому модели такие инструменты отдельно не
    отдаются: она описывает всю связку узлами одного вызова workflow, и
    схема workflow несёт точные схемы потоковых инструментов сессии с
    выбором по имени инструмента. Создаёт его DagCalls; исполняет вызов
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


@dataclass(frozen=True)
class WorkflowStep:
    """Узел вызова workflow и узел DAG, которым он исполняется."""

    node: WorkflowNode
    dag_node: DagNode


@dataclass(frozen=True)
class PlannedCall:
    """Вызов модели, переведённый в описание DAG.

    steps — узлы вызова workflow в порядке описания; у обычного вызова их
    нет, его единственный узел несёт ключ tool_call_id.
    """

    dag: DagSpec
    steps: tuple[WorkflowStep, ...]


class CallDag:
    """Описание DAG одного вызова модели.

    Обычный вызов — DAG из одного узла с ключом tool_call_id и аргументами,
    как назвала модель. Вызов workflow раскрывается в свои узлы: ключ узла
    DAG — tool_call_id вызова и номер узла (под ним идут журнал и шаг ленты
    узла), а имя, данное узлу моделью, едет в title — им узел называется в
    текстах отказов. Имя DAG — tool_call_id вызова.
    """

    def __init__(self, pumps: frozenset[str]) -> None:
        self._pumps = pumps

    def of(self, call: ToolCall) -> PlannedCall:
        """DAG вызова.

        Ошибки:
        StreamPlanError — вызов без tool_call_id; вызов workflow не проходит
            свою форму либо зовёт не потоковый инструмент.
        """
        call_id = self._id_of(call)

        if call["name"] != WorkflowTool.NAME:
            node = DagNode(key=call_id, tool=call["name"], args=dict(call["args"]))
            dag = DagSpec(name=call_id, version=1, nodes=[node])

            return PlannedCall(dag=dag, steps=())

        steps = tuple(self._steps(call_id, call))

        nodes: list[DagNode] = []
        for step in steps:
            nodes.append(step.dag_node)

        return PlannedCall(
            dag=DagSpec(name=call_id, version=1, nodes=nodes), steps=steps
        )

    def _steps(self, call_id: str, call: ToolCall) -> Iterator[WorkflowStep]:
        try:
            parsed = WorkflowCall.model_validate(call["args"])
        except ValidationError as exc:
            msg = (
                f"workflow call {call_id!r} does not match its schema: "
                f"{self._problems(exc)}"
            )
            raise StreamPlanError(msg) from exc

        for index, node in enumerate(parsed.nodes):
            if node.tool not in self._pumps:
                msg = (
                    f"workflow call {call_id!r}: node {node.key!r} names tool "
                    f"{node.tool!r}, expected one of the stream tools "
                    f"{sorted(self._pumps)}; a tool without stream ports is "
                    "called directly, not through workflow"
                )
                raise StreamPlanError(msg)

            dag_node = DagNode(
                key=f"{call_id}_{index}",
                tool=node.tool,
                args=dict(node.args),
                title=node.key,
            )

            yield WorkflowStep(node=node, dag_node=dag_node)

    @staticmethod
    def _id_of(call: ToolCall) -> str:
        key = call["id"]
        if not key:
            name = call["name"]
            msg = f"dag plan: call of {name!r} has no tool call id"
            raise StreamPlanError(msg)

        return key

    @staticmethod
    def _problems(error: ValidationError) -> str:
        """Нарушения формы одной строкой: путь поля и причина, без значений."""
        parts: list[str] = []
        for problem in error.errors():
            path: list[str] = []
            for segment in problem["loc"]:
                path.append(str(segment))

            if not path:
                parts.append(str(problem["msg"]))
                continue

            parts.append(f"{'.'.join(path)}: {problem['msg']}")

        return "; ".join(parts)


class DagCalls:
    """Исполнение вызовов инструментов модели в сессии чата через DAG.

    Каждый вызов ответа модели приходит сюда из middleware агента со своим
    конфигом (callbacks ленты), строит свой DAG и ждёт его итог: обычный
    вызов — итог своего узла, вызов workflow — итоги всех своих узлов одним
    результатом. Отказ плана — ошибка этому вызову; соседние вызовы ответа
    идут своим чередом. Потоковый инструмент, вызванный мимо workflow,
    получает отказ с подсказкой. Создаётся на сессию из её инструментов и
    секции [stream_groups]; модели отдаёт инструменты без портов и workflow
    (model_tools), подготовке хода — исполнитель над её инструментами
    (runner).
    """

    def __init__(self, tools: Sequence[BaseTool], config: StreamGroupsConfig) -> None:
        self._config = config

        by_name: dict[str, BaseTool] = {}
        plain: list[BaseTool] = []
        pumps: list[BaseTool] = []
        for tool in tools:
            by_name[tool.name] = tool
            if ToolStreamSpecs.of(tool.name).streaming():
                pumps.append(tool)
                continue

            plain.append(tool)

        self._plain = plain
        self._pumps = pumps
        self._names = frozenset(by_name)
        self._pump_names = self._names_of(pumps)
        self._dags = CallDag(self._pump_names)
        self._runner = DagRunner(by_name, config.timings(), config.pipe_bytes)

    def owns(self, tool: str) -> bool:
        """Вызов инструмента идёт сюда: инструмент сессии либо workflow."""
        if tool == WorkflowTool.NAME:
            return bool(self._pumps)

        return tool in self._names

    def model_tools(self) -> list[BaseTool]:
        """Инструменты для модели: без портов — как есть, потоковые — одним
        инструментом workflow."""
        offered = list(self._plain)
        if self._pumps:
            offered.append(WorkflowTool(self._pumps).build())

        return offered

    def history_names(self) -> frozenset[str]:
        """Имена вызовов, которые история сессии считает своими: инструменты
        сессии и workflow."""
        return self._names | {WorkflowTool.NAME}

    def runner(self, tools: Sequence[BaseTool]) -> DagRunner:
        """Исполнитель DAG над инструментами tools с настройками сессии."""
        by_name: dict[str, BaseTool] = {}
        for tool in tools:
            by_name[tool.name] = tool

        return DagRunner(by_name, self._config.timings(), self._config.pipe_bytes)

    async def message_for(
        self, call: ToolCall, config: RunnableConfig | None
    ) -> ToolMessage:
        """Итог вызова call как сообщение модели."""
        if call["name"] in self._pump_names:
            return self._outside_workflow(call)

        try:
            planned = self._dags.of(call)
            handle = self._runner.start(planned.dag, config)
        except (StreamPlanError, DagRunError) as exc:
            logger.warning("dag plan refused: %s", exc)
            failure = ErrorResult(
                message=str(exc), error_kind=StreamCallKind.PLAN_REFUSED
            )
            return self._failed(call, failure)

        try:
            if planned.steps:
                return await self._workflow_message(handle, call, planned.steps)

            outcome = await handle.result(planned.dag.nodes[0].key)
        except asyncio.CancelledError:
            handle.cancel()
            raise

        return outcome.message(str(call["id"]))

    async def _workflow_message(
        self, handle: DagHandle, call: ToolCall, steps: Sequence[WorkflowStep]
    ) -> ToolMessage:
        """Итоги узлов вызова workflow одним сообщением модели."""
        nodes: list[WorkflowNodeResult] = []
        failed = False
        for step in steps:
            outcome = await handle.result(step.dag_node.key)
            if outcome.failed():
                failed = True

            nodes.append(self._node_result(step, outcome))

        result = WorkflowResult(nodes=nodes, ok=not failed)
        content, artifact = result.packed()

        status: Literal["success", "error"] = "success"
        if failed:
            status = "error"

        return ToolMessage(
            content=content,
            artifact=artifact,
            name=WorkflowTool.NAME,
            tool_call_id=str(call["id"]),
            status=status,
        )

    @staticmethod
    def _node_result(step: WorkflowStep, outcome: NodeOutcome) -> WorkflowNodeResult:
        return WorkflowNodeResult(
            key=step.node.key,
            call_id=step.dag_node.key,
            tool=step.node.tool,
            args=dict(step.node.args),
            errored=outcome.errored,
            content=outcome.content,
            result=outcome.artifact,
        )

    def _outside_workflow(self, call: ToolCall) -> ToolMessage:
        name = call["name"]
        msg = (
            f"tool {name!r} reads or writes stream channels and runs only as a "
            f"node of {WorkflowTool.NAME!r}: call {WorkflowTool.NAME!r} with a "
            f"node for {name!r} and nodes for the tools on the other ends of its "
            "channels"
        )
        failure = ErrorResult(message=msg, error_kind=StreamCallKind.OUTSIDE_WORKFLOW)

        return self._failed(call, failure)

    @staticmethod
    def _names_of(tools: Sequence[BaseTool]) -> frozenset[str]:
        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        return frozenset(names)

    @staticmethod
    def _failed(call: ToolCall, failure: FailureResult) -> ToolMessage:
        content, artifact = failure.packed()

        return ToolMessage(
            content=content,
            artifact=artifact,
            name=call["name"],
            tool_call_id=str(call["id"]),
            status="error",
        )
