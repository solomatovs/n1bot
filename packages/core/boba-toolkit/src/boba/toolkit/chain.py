"""План каналов и результаты группы инструментов без запуска процессов.

Ошибки:
ChainMismatchError — порты источника и приёмника несовместимы.
StreamPlanError — граф каналов нарушает контракт.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from boba.toolkit.launcher import (
    LauncherError,
)
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpec
from boba.toolkit.result import (
    DetailBlock,
    Fact,
    FactsBlock,
    FailureResult,
    FailureResultField,
    NoteBlock,
)

__all__ = [
    "ChainCheck",
    "ChainMismatchError",
    "ChannelRoute",
    "GroupCall",
    "GroupFailureResult",
    "GroupVerdict",
    "ReaderRef",
    "StreamFailureKind",
    "StreamInput",
    "StreamNode",
    "StreamOutput",
    "StreamPlan",
    "StreamPlanError",
    "StreamTimings",
]


class GroupCall(BaseModel):
    """Вызов группы каналов: инструмент и id вызова модели."""

    model_config = ConfigDict(frozen=True)

    tool: str
    call_id: str


class GroupFailureResult(FailureResult):
    """Срыв группы вызовов, связанных каналами: «все или никто».

    Вызов, который сорвал группу (own), показывает свою ошибку целиком и
    пометку, что остальные вызовы ничего не зафиксировали. Остальные вызовы
    получают короткий итог со ссылкой на сорвавшийся вызов — копия чужой
    ошибки в каждом шаге ничего не объясняет. Без origin группу сорвала она
    сама (застой, срок открытия каналов), и причину видят все.
    """

    kind: Literal["stream_group_failure"] = "stream_group_failure"
    cause: FailureResultField
    origin: GroupCall | None = None
    calls: Sequence[GroupCall] = ()
    own: bool = False

    def headline(self) -> str:
        if self.origin is None:
            return self.cause.headline()

        if self.own:
            return self.cause.headline()

        return f"stopped: {self.origin.tool} failed in the stream group"

    def details(self) -> Sequence[DetailBlock]:
        if self.origin is None:
            return (*self.cause.details(), NoteBlock(text=self._stopped_note()))

        if self.own:
            return (*self.cause.details(), NoteBlock(text=self._stopped_note()))

        facts = [
            Fact(
                key="failed call", value=f"{self.origin.tool} ({self.origin.call_id})"
            ),
            Fact(key="cause", value=self.cause.headline()),
        ]

        return (FactsBlock(facts=facts), NoteBlock(text="nothing was committed"))

    def trace(self) -> str:
        if self.origin is None:
            return self.cause.trace()

        if self.own:
            return self.cause.trace()

        return ""

    def _stopped_note(self) -> str:
        others = list(self._others())
        if not others:
            return "the stream group was stopped, nothing was committed"

        joined = ", ".join(others)

        return f"the stream group was stopped, nothing was committed by: {joined}"

    def _others(self) -> Iterator[str]:
        for call in self.calls:
            if call == self.origin:
                continue

            yield call.tool


class ChainMismatchError(LauncherError):
    """Выходной порт источника не подходит входному порту приёмника."""


class StreamPlanError(LauncherError):
    """Граф каналов группы нарушен: запускать его нельзя."""


class StreamFailureKind(StrEnum):
    """Коды error_kind отказа плана и срывов группы каналов."""

    PLAN_REFUSED = "stream_plan_refused"
    GROUP_FAILED = "stream_group_failed"
    CHANNEL_FAILED = "stream_channel_failed"
    STALLED = "stream_stalled"
    NOT_OPENED = "stream_not_opened"


class ChainCheck:
    """Сверка стыковки выходного порта источника со входным портом приёмника
    до запуска.

    Правила: сырой канал совместим только с сырым (модельный поток
    кадрирован — его рамки попали бы в данные сырого входа, а сырому потоку
    нечем пройти модельную валидацию); модельные порты совместимы, когда
    каждый kind выхода объявлен на входе.
    """

    @classmethod
    def ensure(cls, outbound: PortDecl, inbound: PortDecl) -> None:
        if outbound.direction is not PortDirection.OUTBOUND:
            msg = (
                f"source port {outbound.name!r} is {outbound.direction}, "
                "a channel is written by an outbound port"
            )
            raise ChainMismatchError(msg)

        if inbound.direction is not PortDirection.INBOUND:
            msg = (
                f"sink port {inbound.name!r} is {inbound.direction}, "
                "a channel is read by an inbound port"
            )
            raise ChainMismatchError(msg)

        if inbound.omnivore:
            return

        if outbound.raw and inbound.raw:
            return

        if outbound.raw or inbound.raw:
            msg = (
                f"raw and framed ports do not mix: source {outbound.name!r} "
                f"is {cls._mode(outbound)}, sink {inbound.name!r} is "
                f"{cls._mode(inbound)}"
            )
            raise ChainMismatchError(msg)

        missing = set(outbound.kinds) - set(inbound.kinds)
        if missing:
            listed = ", ".join(sorted(missing))
            msg = (
                f"sink port {inbound.name!r} does not accept kinds "
                f"emitted by {outbound.name!r}: {listed}"
            )
            raise ChainMismatchError(msg)

    @staticmethod
    def _mode(port: PortDecl) -> str:
        if port.raw:
            return "raw"

        return "framed"


@dataclass(frozen=True)
class StreamInput:
    """Вход вызова в плане: входной порт и канал, из которого он читает."""

    port: str
    channel: str


@dataclass(frozen=True)
class StreamOutput:
    """Выход вызова в плане: выходной порт и канал, в который он пишет."""

    port: str
    channel: str


@dataclass(frozen=True)
class StreamNode:
    """Вызов в плане группы: ключ (tool_call_id), инструмент, его порты,
    каналы, которые он пишет, и его входы.

    Порядок каналов одного порта-списка — порядок, который назвала модель.
    pipe_bytes — требуемый буфер пайпов каналов, которые пишет этот вызов:
    размер канала задаёт его писатель; 0 — требования нет. title — имя узла,
    каким его назвал автор описания: им вызов называется в текстах отказов;
    без него называется ключ."""

    key: str
    tool: str
    spec: StreamSpec
    outputs: tuple[StreamOutput, ...]
    inputs: tuple[StreamInput, ...]
    pipe_bytes: int = 0
    title: str = ""

    def label(self) -> str:
        if self.title:
            return f"{self.tool} ({self.title})"

        return f"{self.tool} ({self.key})"


@dataclass(frozen=True)
class ReaderRef:
    """Читатель канала: ключ вызова и позиция входа среди его входов."""

    node: str
    index: int


@dataclass(frozen=True)
class ChannelRoute:
    """Канал плана: его писатель (вызов и позиция выхода среди выходов
    вызова) и читатели в порядке объявления."""

    name: str
    writer: str
    writer_index: int
    readers: tuple[ReaderRef, ...]


class StreamPlan:
    """Граф каналов группы, проверенный до запуска.

    Строится из узлов одного DAG; порядок входов каждого вызова
    приводится к порядку входных портов его подписи — ровно так обёртка
    запуска разложит их по ToolCommand.inputs. Нарушение любого правила —
    StreamPlanError с именами вызовов и каналов.
    """

    def __init__(self, nodes: Sequence[StreamNode]) -> None:
        self._nodes: dict[str, StreamNode] = {}
        for node in nodes:
            self._add(node)

        self._inputs: dict[str, tuple[StreamInput, ...]] = {}
        self._outputs: dict[str, tuple[StreamOutput, ...]] = {}
        for node in self._nodes.values():
            self._inputs[node.key] = self._ordered_inputs(node)
            self._outputs[node.key] = self._ordered_outputs(node)

        self._routes = self._build_routes()
        self._check_ports()
        self._check_cycles()

    def nodes(self) -> tuple[StreamNode, ...]:
        return tuple(self._nodes.values())

    def node(self, key: str) -> StreamNode:
        return self._nodes[key]

    def inputs_of(self, key: str) -> tuple[StreamInput, ...]:
        """Входы вызова в порядке ToolCommand.inputs."""
        return self._inputs[key]

    def outputs_of(self, key: str) -> tuple[StreamOutput, ...]:
        """Выходы вызова в порядке ToolCommand.outputs."""
        return self._outputs[key]

    def routes(self) -> tuple[ChannelRoute, ...]:
        return tuple(self._routes.values())

    def route(self, channel: str) -> ChannelRoute:
        return self._routes[channel]

    def pipe_bytes_of(self, channel: str) -> int:
        """Требуемый буфер пайпов канала: его назвал писатель; 0 — нет."""
        return self._nodes[self._routes[channel].writer].pipe_bytes

    def _add(self, node: StreamNode) -> None:
        if node.key in self._nodes:
            msg = (
                f"stream plan: call id {node.key!r} is used by "
                f"{self._nodes[node.key].tool} and {node.tool}"
            )
            raise StreamPlanError(msg)

        self._nodes[node.key] = node

    def _ordered_inputs(self, node: StreamNode) -> tuple[StreamInput, ...]:
        ports = node.spec.inbound()
        bound = self._ordered(node, ports, node.inputs, "inbound")

        ordered: list[StreamInput] = []
        for port in ports:
            for item in bound[port.name]:
                ordered.append(StreamInput(port=item.port, channel=item.channel))

        return tuple(ordered)

    def _ordered_outputs(self, node: StreamNode) -> tuple[StreamOutput, ...]:
        ports = node.spec.outbound()
        bound = self._ordered(node, ports, node.outputs, "outbound")

        ordered: list[StreamOutput] = []
        for port in ports:
            for item in bound[port.name]:
                ordered.append(StreamOutput(port=item.port, channel=item.channel))

        return tuple(ordered)

    @staticmethod
    def _ordered(
        node: StreamNode,
        ports: Sequence[PortDecl],
        bound: Sequence[StreamInput] | Sequence[StreamOutput],
        side: str,
    ) -> dict[str, list[StreamInput | StreamOutput]]:
        """Каналы вызова по портам одной стороны в порядке подписи.

        Канал чужого порта и второй канал одиночного порта — StreamPlanError.
        """
        declared: dict[str, list[StreamInput | StreamOutput]] = {}
        for port in ports:
            declared[port.name] = []

        for item in bound:
            own = declared.get(item.port)
            if own is None:
                msg = (
                    f"stream plan: {node.label()} binds channel {item.channel!r} "
                    f"to {item.port!r}, its {side} ports are {sorted(declared)}"
                )
                raise StreamPlanError(msg)

            own.append(item)

        for port in ports:
            if port.many:
                continue

            if len(declared[port.name]) > 1:
                msg = (
                    f"stream plan: {side} port {port.name!r} of {node.label()} "
                    f"takes at most one channel, got {len(declared[port.name])}"
                )
                raise StreamPlanError(msg)

        return declared

    def _build_routes(self) -> dict[str, ChannelRoute]:
        writers: dict[str, tuple[str, int]] = {}
        for node in self._nodes.values():
            for index, output in enumerate(self._outputs[node.key]):
                taken = writers.get(output.channel)
                if taken is not None:
                    first = self._nodes[taken[0]]
                    msg = (
                        f"stream plan: channel {output.channel!r} has two "
                        f"writers, {first.label()} and {node.label()}; a "
                        "channel has exactly one writer"
                    )
                    raise StreamPlanError(msg)

                writers[output.channel] = (node.key, index)

        readers: dict[str, list[ReaderRef]] = {}
        for channel in writers:
            readers[channel] = []

        for node in self._nodes.values():
            for index, bound in enumerate(self._inputs[node.key]):
                listed = readers.get(bound.channel)
                if listed is None:
                    msg = (
                        f"stream plan: {node.label()} reads channel "
                        f"{bound.channel!r} in {bound.port!r}, which no node "
                        f"writes; channels written here: {sorted(writers)}. Name "
                        "one of them or add the node that writes this channel"
                    )
                    raise StreamPlanError(msg)

                listed.append(ReaderRef(node=node.key, index=index))

        routes: dict[str, ChannelRoute] = {}
        for channel, (writer, index) in writers.items():
            if not readers[channel]:
                read = list(self._read_channels(readers))
                msg = (
                    f"stream plan: channel {channel!r} written by "
                    f"{self._nodes[writer].label()} has no readers; channels "
                    f"read here: {read}. Add a node that reads it, or drain it "
                    "explicitly with dev_null"
                )
                raise StreamPlanError(msg)

            routes[channel] = ChannelRoute(
                name=channel,
                writer=writer,
                writer_index=index,
                readers=tuple(readers[channel]),
            )

        return routes

    @staticmethod
    def _read_channels(readers: Mapping[str, Sequence[ReaderRef]]) -> Iterator[str]:
        """Каналы, у которых есть хотя бы один читатель, по алфавиту."""
        for name in sorted(readers):
            if readers[name]:
                yield name

    def _check_ports(self) -> None:
        for route in self._routes.values():
            writer = self._nodes[route.writer]
            output = self._outputs[route.writer][route.writer_index]
            outbound = writer.spec.port(output.port)

            for reader in route.readers:
                node = self._nodes[reader.node]
                port = node.spec.port(self._inputs[node.key][reader.index].port)

                try:
                    ChainCheck.ensure(outbound, port)
                except ChainMismatchError as exc:
                    msg = (
                        f"stream plan: channel {route.name!r} from "
                        f"{writer.label()} into {node.label()}: {exc}"
                    )
                    raise StreamPlanError(msg) from exc

    def _check_cycles(self) -> None:
        followers: dict[str, set[str]] = {}
        for node in self._nodes:
            followers[node] = set()

        for route in self._routes.values():
            for reader in route.readers:
                followers[route.writer].add(reader.node)

        visited: set[str] = set()
        for start in self._nodes:
            self._walk(start, followers, visited, [])

    def _walk(
        self,
        key: str,
        followers: Mapping[str, set[str]],
        visited: set[str],
        path: list[str],
    ) -> None:
        if key in path:
            cycle = [*path[path.index(key) :], key]
            labels: list[str] = []
            for item in cycle:
                labels.append(self._nodes[item].label())

            msg = f"stream plan: channels form a cycle: {' -> '.join(labels)}"
            raise StreamPlanError(msg)

        if key in visited:
            return

        path.append(key)
        for follower in sorted(followers[key]):
            self._walk(follower, followers, visited, path)

        path.pop()
        visited.add(key)


@dataclass(frozen=True)
class StreamTimings:
    """Сроки группы: сколько ждать открытия всех вызовов и сколько терпеть
    отсутствие движения данных, пока каналы открыты."""

    open_sec: float
    stall_sec: float
    poll_sec: float


@dataclass(frozen=True)
class GroupVerdict:
    """Итог группы для каждого её вызова: успех либо срыв. stopped — группу
    сорвала остановка хода, а не сбой: вызовы поднимают ToolStopped."""

    failure: GroupFailureResult | None
    stopped: bool

    @property
    def ok(self) -> bool:
        return self.failure is None

    def failure_of(self, key: str) -> GroupFailureResult:
        """Срыв глазами вызова key: сорвал ли группу он сам.

        Ошибки:
        LauncherError — группа прошла, срыва нет.
        """
        if self.failure is None:
            msg = f"stream group verdict for call {key!r}: the group succeeded"
            raise LauncherError(msg)

        own = False
        if origin := self.failure.origin:
            own = origin.call_id == key

        return self.failure.model_copy(update={"own": own})
