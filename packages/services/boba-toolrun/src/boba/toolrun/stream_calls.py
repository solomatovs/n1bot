"""Вызовы инструментов модели через DAG и порт сервера инструментов.

Каждый вызов инструмента исполняет DAG. Инструмент без портов — DAG из
одного узла. Потоковые инструменты (с портами каналов) модель связывает
именами каналов: выходной порт — поле `out="orders"`, входной — поле
`feed="orders"` или список `feeds=["a", "b"]`. Связывает их один вызов
инструмента workflow, узлы которого называют инструменты и их аргументы;
отдельный вызов потокового инструмента остаётся без партнёра и получает
отказ плана. Здесь части этого пути:

- StreamChannelFields заменяет порты в схеме инструмента полями каналов,
  барьер группы StreamGroup из схемы убирает.
- WorkflowTool — инструмент workflow для модели: узел называет инструмент из
  списка сервера и его аргументы; схем инструментов в себе не несёт.
- LocalDagService — реализация порта сервера инструментов ToolServer
  (boba.toolkit.dag) исполнителем в своём процессе: описание DAG по вызовам
  клиента строит CallDag. ToolServers — несколько серверов за одним портом
  с маршрутом по имени инструмента.
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
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from boba.toolkit.chain import (
    CallAmbient,
    StreamFailureKind,
    StreamPlanError,
    StreamTimings,
)
from boba.toolkit.dag import (
    CallDag,
    DagNode,
    DagPlanner,
    DagSpec,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolServer,
    WorkflowFeature,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.entry import EntryErrorKind, ToolArgv
from boba.toolkit.failure import ToolRefusalError
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpecs
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
)
from boba.toolrun.call_id import CallFields
from boba.toolrun.dag_run import DagOutcome, DagRunError, DagRunner
from boba.toolrun.hosted import HostedTool, ToolSchema

__all__ = [
    "LocalDagService",
    "NodeArgs",
    "StreamChannelFields",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
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
        self._schemas = ToolSchema()

    def attach_all(self, tools: Sequence[HostedTool], specs: StreamSpecs) -> None:
        for tool in tools:
            self._attach(tool, specs)

    def _attach(self, tool: HostedTool, specs: StreamSpecs) -> None:
        schema = tool.args_schema
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

        tool.args_schema = self._schemas.rebuild(schema, fields, drop)

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

    Потоковому инструменту нужен партнёр на другом конце канала: модель
    описывает партнёров узлами одного вызова workflow. Узел называет инструмент из
    списка сервера и его аргументы; схем инструментов workflow не несёт —
    они у самих инструментов. Создаёт его LocalDagService; исполняет вызов
    не тело инструмента, а DAG — инструмента хоста за ним нет, есть только
    карточка, чтобы модель видела его среди инструментов сервера.
    """

    NAME: ClassVar[str] = CallDag.WORKFLOW

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
        self._schemas = ToolSchema()

    def card(self) -> ToolCard:
        """Карточка для модели: узлы называют инструменты сервера по имени."""
        return self._schemas.card(
            self.NAME,
            self.DESCRIPTION.strip(),
            self._fields.extended(WorkflowCall),
            None,
        )

    def feature(self) -> WorkflowFeature:
        """Объявление возможности: связку описывает этот инструмент."""
        return WorkflowFeature(tool=self.NAME)


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

    Клиент получает отсюда карточки инструментов для модели и шлёт вызовы.
    Модели отдаются все инструменты как есть; если среди них есть потоковые,
    к ним добавляется workflow. Каждый вызов исполняет свой DAG: вызовы
    одного пакета между собой не связаны, потоковые инструменты связывает
    только вызов workflow. Отказ плана и отказ правила аргументов
    (NodeArgs) — итог-ошибка вызова до старта узлов; отказ плана отдельному
    вызову называет workflow как способ связать инструменты. Выдуманное имя
    получает отказ со списком инструментов. Создаётся из обёрнутых
    инструментов реестра, секции [stream_groups], правил аргументов,
    потоковых деклараций инструментов и пути вызова узла к телу (NodeCalls).
    """

    LONELY_HINT: ClassVar[str] = (
        "Tools joined by channels run only as nodes of one call of the tool "
        "'workflow': a separate call has no partner on the other end of its "
        "channel"
    )

    def __init__(  # noqa: PLR0913 — порт собирается всеми входами исполнителя
        self,
        tools: Sequence[HostedTool],
        config: StreamGroupsConfig,
        rules: Sequence[NodeArgs],
        specs: StreamSpecs,
        ambient: CallAmbient,
        calls: NodeCalls,
    ) -> None:
        self._rules = tuple(rules)
        self._schemas = ToolSchema()

        by_name: dict[str, HostedTool] = {}
        linked: list[str] = []
        for tool in tools:
            by_name[tool.name] = tool
            if specs.of(tool.name).streaming():
                linked.append(tool.name)

        cards = self._schemas.cards_of(tools)
        offered = list(cards.values())

        self._workflow: WorkflowTool | None = None
        if linked:
            self._workflow = WorkflowTool()
            offered.append(self._workflow.card())

        self._offered = offered
        self._outcomes = NodeOutcomes()
        self._dags = CallDag()
        self._runner = DagRunner(
            by_name, cards, specs, ambient, config.timings(), config.pipe_bytes, calls
        )

    def tools(self) -> Sequence[ToolCard]:
        return self._offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        declared: dict[str, Mapping[str, object]] = {}
        for rule in self._rules:
            declared.update(rule.features())

        if self._workflow is not None:
            feature = self._workflow.feature()
            declared[WorkflowFeature.ID] = feature.settings()

        return declared

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._single(call)))

        return pending

    async def _single(self, call: DagNode) -> NodeOutcome:
        """Итог вызова, который идёт своим DAG: обычный инструмент или workflow."""
        try:
            dag = self._dags.of(call)
        except StreamPlanError as exc:
            return self._refused(call, self._plan_refusal(exc, call))

        outcome = await self._outcome(dag, call)
        if isinstance(outcome, FailureResult):
            return self._refused(call, outcome)

        if call.tool != WorkflowTool.NAME:
            return outcome.nodes[0]

        nodes: list[WorkflowNodeResult] = []
        for node, ended in zip(dag.nodes, outcome.nodes, strict=True):
            nodes.append(self._node_result(node, ended))

        failed = not outcome.ok()

        result = WorkflowResult(nodes=nodes, ok=not failed)

        return self._outcomes.of(call, result, failed)

    async def _outcome(
        self, dag: DagSpec, call: DagNode
    ) -> DagOutcome | FailureResult:
        """Итог DAG вызова call; отказ плана или правила аргументов — отказ
        до старта."""
        try:
            await self._check_args(dag)
            handle = self._runner.start(dag)
        except (StreamPlanError, DagRunError) as exc:
            return self._plan_refusal(exc, call)
        except ToolRefusalError as exc:
            logger.warning("dag arguments refused: %s", exc)
            return exc.failure()

        try:
            return await handle.outcome()
        except asyncio.CancelledError:
            handle.cancel()
            raise

    def _plan_refusal(self, exc: Exception, call: DagNode) -> ErrorResult:
        """Отказ плана вызову call; отдельному вызову, которому не хватило
        партнёра по каналу, отказ называет инструмент-связку."""
        logger.warning("dag plan refused: %s", exc)

        message = str(exc)
        if self._lonely(exc, call):
            message = f"{message}. {self.LONELY_HINT}"

        return ErrorResult(message=message, error_kind=StreamFailureKind.PLAN_REFUSED)

    def _lonely(self, exc: Exception, call: DagNode) -> bool:
        if self._workflow is None:
            return False

        if call.tool == WorkflowTool.NAME:
            return False

        return isinstance(exc, StreamPlanError)

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

    def _refused(self, call: DagNode, failure: FailureResult) -> NodeOutcome:
        return self._outcomes.refused(call, failure)


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
            for card in server.tools():
                self._claim(card.name, index)

        self._unknown = UnknownTool(frozenset(self._owner))
        self._outcomes = NodeOutcomes()

    def tools(self) -> Sequence[ToolCard]:
        offered: list[ToolCard] = []
        for server in self._servers:
            offered.extend(server.tools())

        return offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        declared: dict[str, Mapping[str, object]] = {}
        for server in self._servers:
            declared.update(server.features())

        return declared

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        routed: dict[int, list[int]] = {}
        pending: dict[int, asyncio.Future[NodeOutcome]] = {}
        for position, call in enumerate(calls):
            owner = self._owner.get(call.tool)
            if owner is None:
                refusal = self._unknown.refusal(call.tool)
                refused = self._outcomes.refused(call, refusal)
                pending[position] = self._outcomes.settled(refused)
                continue

            routed.setdefault(owner, []).append(position)

        for owner, positions in routed.items():
            batch: list[DagNode] = []
            for position in positions:
                batch.append(calls[position])

            accepted = await self._servers[owner].submit(batch)
            for position, future in zip(positions, accepted, strict=True):
                pending[position] = future

        ordered: list[asyncio.Future[NodeOutcome]] = []
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
