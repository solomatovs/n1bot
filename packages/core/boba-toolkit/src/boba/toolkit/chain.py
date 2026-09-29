"""Группа связанных каналами вызовов: план, раздача потока, барьер, срыв.

Модель в одном ответе связывает вызовы каналами: у канала один писатель
(выходной порт вызова) и сколько угодно читателей (входы других вызовов).
Здесь живёт весь механизм исполнения такого графа:

- StreamPlan — граф каналов, проверенный до запуска: писатель у канала
  один, читатели есть, kind'ы портов стыкуются (ChainCheck), циклов нет.
- ChannelFanOut — раздача выхода писателя читателям силами ядра: цепочка
  звеньев tee(2) + splice(2), данные в userspace не поднимаются.
- StreamGroupRun — исполнение группы по правилу «все или никто»: EOF
  читатели получают только после успешного итога писателя, барьер
  StreamGroup отпускается, когда до него дошли все, сбой любого вызова
  или застой данных срывает всю группу.
- NodeSlot / PipelineSlot — роль одного вызова в группе для обёртки
  запуска (ToolProcessWrap): она открывает вызов потоково и отдаёт группе
  его каналы.

Backpressure сквозной: медленный читатель останавливает писателя через
полные буферы пайпов, раздача идёт в темпе самого медленного.

Ошибки:
ChainMismatchError — выходной порт не стыкуется со входным.
StreamPlanError — граф каналов нарушен: писателей не один, читателей нет,
    вход без канала, цикл, лишние входы.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import struct
import termios
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from boba.toolkit.launcher import (
    LauncherError,
    TappedCall,
    ToolCall,
    ToolOutcome,
)
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpec
from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand
from boba.toolkit.pump import PipePlumbing

__all__ = [
    "ChainCheck",
    "ChainMismatchError",
    "ChannelFanOut",
    "ChannelRoute",
    "GroupVerdict",
    "NodeSlot",
    "PipeTee",
    "PipelineSlot",
    "ReaderRef",
    "StreamFailureKind",
    "StreamGroupRun",
    "StreamInput",
    "StreamNode",
    "StreamPlan",
    "StreamPlanError",
    "StreamTimings",
    "TappedCall",
]

logger = logging.getLogger(__name__)


class ChainMismatchError(LauncherError):
    """Выходной порт источника не подходит входному порту приёмника."""


class StreamPlanError(LauncherError):
    """Граф каналов группы нарушен: запускать его нельзя."""


class StreamFailureKind(StrEnum):
    """Kind отказа вызова, чья группа сорвалась."""

    GROUP_FAILED = "stream_group_failed"


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


class PipeTee:
    """tee(2) из libc: копия данных пайпа в другой пайп без их изъятия.

    В модуле os обёртки нет, поэтому вызов идёт через ctypes. Ядро делит
    страницы буфера между пайпами ссылками — данные не копируются.
    Используется раздачей ChannelFanOut.
    """

    def __init__(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self._tee = libc.tee
        self._tee.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_uint,
        ]
        self._tee.restype = ctypes.c_ssize_t

    def tee(self, source_fd: int, sink_fd: int, count: int) -> int:
        """Скопировать до count байт; 0 — источник пуст и закрыт писателем."""
        moved = self._tee(source_fd, sink_fd, count, 0)
        if moved >= 0:
            return moved

        errno = ctypes.get_errno()
        msg = f"tee({source_fd} -> {sink_fd}) failed: {os.strerror(errno)}"
        raise OSError(errno, msg)


class ChannelFanOut:
    """Раздача выхода писателя всем читателям канала без подъёма данных в
    userspace.

    Цепочка звеньев, по одному на читателя: звено tee'ит данные своего
    входного пайпа в пайп читателя (данные остаются во входе) и затем
    splice'ом переносит ровно столько же байт в промежуточный пайп
    следующего звена; последнее звено отдаёт данные читателю чистым splice.
    tee может скопировать меньше просимого и не умеет продолжать с
    середины, поэтому перенос вниз идёт ровно на скопированное — следующий
    tee начинает с головы пайпа. Каждое звено — свой поток; скорость задаёт
    самый медленный читатель.

    Читатель, закрывший вход раньше конца потока, от раздачи отцепляется:
    его звено дальше только передаёт данные вниз по цепочке (последнее —
    дочитывает в /dev/null), остальные читатели получают поток целиком.
    Сбой ли это, решает итог самого читателя, а не раздача.

    Дескрипторы читателей раздача не закрывает: EOF читателям даёт группа
    (StreamGroupRun), когда писатель закончил успешно. Вход писателя и
    промежуточные пайпы закрываются здесь. Прочие ошибки звеньев уходят в
    on_error; конец всех звеньев — в on_drained.
    """

    CHUNK_BYTES: ClassVar[int] = 1 << 20

    def __init__(  # noqa: PLR0913
        self,
        channel: str,
        source_fd: int,
        reader_fds: Sequence[int],
        tee: PipeTee,
        on_error: Callable[[str], None],
        on_drained: Callable[[], None],
    ) -> None:
        self._channel = channel
        self._source_fd = source_fd
        self._reader_fds = tuple(reader_fds)
        self._tee = tee
        self._on_error = on_error
        self._on_drained = on_drained
        self._lock = threading.Lock()
        self._moved = [0] * len(self._reader_fds)
        self._running = len(self._reader_fds)

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def reader_fds(self) -> tuple[int, ...]:
        return self._reader_fds

    def moved(self) -> int:
        """Сколько байт уже отдано читателям, по всем звеньям."""
        return sum(self._moved)

    def start(self) -> None:
        for hop in self._hops():
            hop.start()

    def _hops(self) -> Iterator[threading.Thread]:
        last = len(self._reader_fds) - 1
        inbound = self._source_fd

        for index, reader_fd in enumerate(self._reader_fds):
            if index == last:
                yield self._thread(index, self._last_hop, inbound, reader_fd, -1)
                return

            next_r, next_w = os.pipe()
            PipePlumbing.widen(next_w)
            yield self._thread(index, self._tee_hop, inbound, reader_fd, next_w)
            inbound = next_r

    def _thread(
        self,
        index: int,
        hop: Callable[[int, int, int, int], None],
        inbound: int,
        reader_fd: int,
        next_fd: int,
    ) -> threading.Thread:
        return threading.Thread(
            target=self._run_hop,
            args=(index, hop, inbound, reader_fd, next_fd),
            name=f"stream-fanout:{self._channel}:{index}",
            daemon=True,
        )

    def _run_hop(
        self,
        index: int,
        hop: Callable[[int, int, int, int], None],
        inbound: int,
        reader_fd: int,
        next_fd: int,
    ) -> None:
        try:
            hop(index, inbound, reader_fd, next_fd)
        except OSError as exc:
            self._on_error(
                f"channel {self._channel!r}: delivering to reader #{index} "
                f"(fd {reader_fd}) failed after {self._moved[index]} bytes: {exc}"
            )
        finally:
            with suppress(OSError):
                os.close(inbound)

            if next_fd >= 0:
                with suppress(OSError):
                    os.close(next_fd)

            self._hop_ended()

    def _tee_hop(self, index: int, inbound: int, reader_fd: int, next_fd: int) -> None:
        while True:
            try:
                copied = self._tee.tee(inbound, reader_fd, self.CHUNK_BYTES)
            except BrokenPipeError:
                self._detached(index)
                self._forward(index, inbound, next_fd)
                return

            if copied == 0:
                return

            self._splice_exact(inbound, next_fd, copied)
            self._moved[index] += copied

    def _last_hop(self, index: int, inbound: int, reader_fd: int, _next: int) -> None:
        try:
            self._forward(index, inbound, reader_fd)
        except BrokenPipeError:
            self._detached(index)
            self._discard(index, inbound)

    def _forward(self, index: int, inbound: int, sink_fd: int) -> None:
        while True:
            moved = os.splice(inbound, sink_fd, self.CHUNK_BYTES)
            if moved == 0:
                return

            self._moved[index] += moved

    def _discard(self, index: int, inbound: int) -> None:
        """Дочитать поток в никуда: писатель не должен встать на полном пайпе."""
        sink = os.open(os.devnull, os.O_WRONLY)
        try:
            self._forward(index, inbound, sink)
        finally:
            os.close(sink)

    def _detached(self, index: int) -> None:
        logger.info(
            "channel %s: reader #%d closed its input after %d bytes, "
            "the rest goes past it",
            self._channel,
            index,
            self._moved[index],
        )

    def _splice_exact(self, source_fd: int, sink_fd: int, count: int) -> None:
        remaining = count
        while remaining:
            moved = os.splice(source_fd, sink_fd, remaining)
            if moved == 0:
                msg = (
                    f"channel {self._channel!r}: pipe ended with {remaining} of "
                    f"{count} tee'd bytes not moved down the chain"
                )
                raise OSError(msg)

            remaining -= moved

    def _hop_ended(self) -> None:
        with self._lock:
            self._running -= 1
            drained = self._running == 0

        if drained:
            self._on_drained()


@dataclass(frozen=True)
class StreamInput:
    """Вход вызова в плане: входной порт и канал, из которого он читает."""

    port: str
    channel: str


@dataclass(frozen=True)
class StreamNode:
    """Вызов в плане группы: ключ (tool_call_id), инструмент, его порты,
    канал, который он пишет (None — вызов только читает), и его входы.

    Порядок входов одного порта-списка — порядок, который назвала модель."""

    key: str
    tool: str
    spec: StreamSpec
    output: str | None
    inputs: tuple[StreamInput, ...]

    def label(self) -> str:
        return f"{self.tool} ({self.key})"


@dataclass(frozen=True)
class ReaderRef:
    """Читатель канала: ключ вызова и позиция входа среди его входов."""

    node: str
    index: int


@dataclass(frozen=True)
class ChannelRoute:
    """Канал плана: его писатель и читатели в порядке объявления."""

    name: str
    writer: str
    readers: tuple[ReaderRef, ...]


class StreamPlan:
    """Граф каналов группы, проверенный до запуска.

    Строится из вызовов одного ответа модели; порядок входов каждого вызова
    приводится к порядку входных портов его подписи — ровно так обёртка
    запуска разложит их по ToolCommand.inputs. Нарушение любого правила —
    StreamPlanError с именами вызовов и каналов.
    """

    def __init__(self, nodes: Sequence[StreamNode]) -> None:
        self._nodes: dict[str, StreamNode] = {}
        for node in nodes:
            self._add(node)

        self._inputs: dict[str, tuple[StreamInput, ...]] = {}
        for node in self._nodes.values():
            self._inputs[node.key] = self._ordered_inputs(node)

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

    def routes(self) -> tuple[ChannelRoute, ...]:
        return tuple(self._routes.values())

    def route(self, channel: str) -> ChannelRoute:
        return self._routes[channel]

    def _add(self, node: StreamNode) -> None:
        if node.key in self._nodes:
            msg = (
                f"stream plan: call id {node.key!r} is used by "
                f"{self._nodes[node.key].tool} and {node.tool}"
            )
            raise StreamPlanError(msg)

        if node.output is not None:
            if node.spec.outbound() is None:
                msg = (
                    f"stream plan: {node.label()} writes channel {node.output!r} "
                    "but declares no outbound port"
                )
                raise StreamPlanError(msg)

        if len(node.inputs) > ToolCommand.MAX_INPUTS:
            msg = (
                f"stream plan: {node.label()} reads {len(node.inputs)} channels, "
                f"a call takes at most {ToolCommand.MAX_INPUTS}"
            )
            raise StreamPlanError(msg)

        self._nodes[node.key] = node

    def _ordered_inputs(self, node: StreamNode) -> tuple[StreamInput, ...]:
        declared: set[str] = set()
        for port in node.spec.inbound():
            declared.add(port.name)

        for bound in node.inputs:
            if bound.port not in declared:
                msg = (
                    f"stream plan: {node.label()} binds channel {bound.channel!r} "
                    f"to {bound.port!r}, its inbound ports are {sorted(declared)}"
                )
                raise StreamPlanError(msg)

        ordered: list[StreamInput] = []
        for port in node.spec.inbound():
            bound = self._bound_to(node, port.name)
            self._check_count(node, port, len(bound))
            ordered.extend(bound)

        return tuple(ordered)

    @staticmethod
    def _bound_to(node: StreamNode, port: str) -> list[StreamInput]:
        bound: list[StreamInput] = []
        for item in node.inputs:
            if item.port == port:
                bound.append(item)

        return bound

    @staticmethod
    def _check_count(node: StreamNode, port: PortDecl, count: int) -> None:
        """Одиночный порт читает не больше одного канала; без канала его вход
        пуст."""
        if port.many:
            return

        if count > 1:
            msg = (
                f"stream plan: inbound port {port.name!r} of {node.label()} "
                f"reads at most one channel, got {count}"
            )
            raise StreamPlanError(msg)

    def _build_routes(self) -> dict[str, ChannelRoute]:
        writers: dict[str, str] = {}
        for node in self._nodes.values():
            if node.output is None:
                continue

            if node.output in writers:
                first = self._nodes[writers[node.output]]
                msg = (
                    f"stream plan: channel {node.output!r} has two writers, "
                    f"{first.label()} and {node.label()}; a channel has "
                    "exactly one writer"
                )
                raise StreamPlanError(msg)

            writers[node.output] = node.key

        readers: dict[str, list[ReaderRef]] = {}
        for channel in writers:
            readers[channel] = []

        for node in self._nodes.values():
            for index, bound in enumerate(self._inputs[node.key]):
                listed = readers.get(bound.channel)
                if listed is None:
                    msg = (
                        f"stream plan: {node.label()} reads channel "
                        f"{bound.channel!r}, no call of the response writes it; "
                        f"written channels: {sorted(writers)}"
                    )
                    raise StreamPlanError(msg)

                listed.append(ReaderRef(node=node.key, index=index))

        routes: dict[str, ChannelRoute] = {}
        for channel, writer in writers.items():
            if not readers[channel]:
                msg = (
                    f"stream plan: channel {channel!r} written by "
                    f"{self._nodes[writer].label()} has no readers"
                )
                raise StreamPlanError(msg)

            routes[channel] = ChannelRoute(
                name=channel, writer=writer, readers=tuple(readers[channel])
            )

        return routes

    def _check_ports(self) -> None:
        for route in self._routes.values():
            writer = self._nodes[route.writer]
            outbound = writer.spec.outbound()
            if outbound is None:
                continue

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
    """Итог группы для каждого её вызова: успех либо текст срыва. stopped —
    группу сорвала остановка хода, а не сбой: вызовы поднимают ToolStopped."""

    ok: bool
    message: str
    stopped: bool


class NodeState(StrEnum):
    """Состояние вызова в группе."""

    PENDING = "pending"
    OPEN = "open"
    AT_GATE = "at_gate"
    DONE = "done"
    FAILED = "failed"

    def terminal(self) -> bool:
        return self in (NodeState.DONE, NodeState.FAILED)


class _NodeRun:
    """Живое состояние одного вызова группы: вызов, его дескрипторы и роль."""

    def __init__(self, node: StreamNode) -> None:
        self.node = node
        self.state = NodeState.PENDING
        self.call: ToolCall | None = None
        self.frames_fd = -1
        self.input_fds: tuple[int, ...] = ()


class StreamGroupRun:
    """Исполнение группы связанных каналами вызовов по правилу «все или никто».

    Создаётся на ответ модели по проверенному плану (StreamPlan); каждый
    вызов получает свою ручку NodeSlot и отдаёт через неё группе открытый
    вызов и дескрипторы каналов. Когда открылись все вызовы, группа
    запускает раздачу каждого канала (ChannelFanOut). Правила:

    - EOF читатель канала получает, только когда писатель закончил успешно
      и раздача опустела; оборванный поток за целый не сойдёт.
    - Барьер StreamGroup отпускается, когда каждый вызов стоит на барьере
      или успешно закончился.
    - Сбой любого вызова, раздачи, неоткрытие вызова в срок и застой данных
      срывают группу: барьеры отказывают, живые вызовы добиваются, и лишь
      после этого читателям закрываются входы. Каждый вызов группы получает
      один и тот же текст срыва с его причиной.

    Координатор живёт своим потоком: следит за сроком открытия и застоем.
    """

    def __init__(self, plan: StreamPlan, timings: StreamTimings) -> None:
        self._plan = plan
        self._timings = timings
        self._tee = PipeTee()
        self._cond = threading.Condition()
        self._runs: dict[str, _NodeRun] = {}
        for node in plan.nodes():
            self._runs[node.key] = _NodeRun(node)

        self._fanouts: dict[str, ChannelFanOut] = {}
        self._drained: set[str] = set()
        self._closed: set[str] = set()
        self._wired = False
        self._released = False
        self._failure = ""
        self._stopped = False
        self._victims: set[str] = set()
        self._aborted = False
        self._opened_at = time.monotonic()

        self._coordinator = threading.Thread(
            target=self._coordinate, name="stream-group", daemon=True
        )
        self._coordinator.start()

    def slot(self, key: str) -> NodeSlot:
        """Ручка вызова key для обёртки запуска."""
        node = self._plan.node(key)

        counts: dict[str, int] = {}
        for port in node.spec.inbound():
            counts[port.name] = 0

        for bound in self._plan.inputs_of(key):
            counts[bound.port] += 1

        return NodeSlot(self, key, counts, has_downstream=node.output is not None)

    def refuse(self, key: str, cause: str) -> None:
        """Вызов key не дошёл до запуска (права, аргументы): группа срывается."""
        self.done(key, ok=False, cause=cause, stopped=False)

    def abort(self, cause: str) -> None:
        """Сорвать группу снаружи (остановка хода)."""
        self._fail(cause, stopped=False)

    def finished(self) -> bool:
        """Группа кончилась: все вызовы закончились, каналы закрыты."""
        with self._cond:
            return self._settled()

    def labels(self) -> list[str]:
        labels: list[str] = []
        for run in self._runs.values():
            labels.append(run.node.label())

        return labels

    def attach(
        self, key: str, call: ToolCall, outputs: Sequence[int], inputs: Sequence[int]
    ) -> None:
        """Вызов открыт: группа получает его и дескрипторы каналов."""
        run = self._runs[key]

        with self._cond:
            failed = bool(self._failure)
            if not failed:
                run.call = call
                run.state = NodeState.OPEN
                run.input_fds = tuple(inputs)
                for fd in outputs:
                    run.frames_fd = fd

            fanouts: list[ChannelFanOut] = []
            if self._all_open():
                self._wired = True
                fanouts = self._register_fanouts()

            self._cond.notify_all()

        if failed:
            self._discard(call, outputs, inputs)
            return

        call.gate().claim(lambda: self._at_gate(key))

        for fanout in fanouts:
            fanout.start()

        if fanouts:
            logger.info(
                "stream group %s: wired %d channels", self.labels(), len(fanouts)
            )

    def done(self, key: str, *, ok: bool, cause: str, stopped: bool) -> None:
        """Вызов закончился: успешно либо с причиной сбоя; stopped — его
        остановил ход, а не группа."""
        run = self._runs[key]

        with self._cond:
            if run.state.terminal():
                return

            run.state = NodeState.DONE
            if not ok:
                run.state = NodeState.FAILED

            self._cond.notify_all()

        if not ok:
            self._fail(f"{run.node.label()}: {cause}", stopped=stopped)
            return

        self._close_finished_channels()
        self._maybe_release()

    def stopped_by_group(self, key: str) -> bool:
        """Вызов key добила сама группа при срыве."""
        with self._cond:
            return key in self._victims

    def verdict(self) -> GroupVerdict:
        """Дождаться конца группы: все вызовы закончились, каналы закрыты."""
        with self._cond:
            while not self._settled():
                self._cond.wait()

            failure = self._failure
            stopped = self._stopped

        if not failure:
            return GroupVerdict(ok=True, message="", stopped=False)

        calls = ", ".join(self.labels())
        return GroupVerdict(
            ok=False,
            message=(
                f"stream group failed, no call commits: {failure}; "
                f"calls of the group: {calls}"
            ),
            stopped=stopped,
        )

    def _all_open(self) -> bool:
        for run in self._runs.values():
            if run.state is NodeState.PENDING:
                return False

        return not self._wired

    def _register_fanouts(self) -> list[ChannelFanOut]:
        """Раздачи всех каналов; зовётся под замком группы, потоки стартуют
        снаружи."""
        fanouts: list[ChannelFanOut] = []
        for route in self._plan.routes():
            fanout = self._fanout_of(route)
            self._fanouts[route.name] = fanout
            fanouts.append(fanout)

        return fanouts

    def _fanout_of(self, route: ChannelRoute) -> ChannelFanOut:
        source_fd = self._runs[route.writer].frames_fd

        reader_fds: list[int] = []
        for reader in route.readers:
            reader_fds.append(self._runs[reader.node].input_fds[reader.index])

        channel = route.name
        return ChannelFanOut(
            channel,
            source_fd,
            reader_fds,
            self._tee,
            on_error=self._fanout_failed,
            on_drained=lambda: self._drain(channel),
        )

    def _fanout_failed(self, cause: str) -> None:
        self._fail(cause, stopped=False)

    def _drain(self, channel: str) -> None:
        with self._cond:
            self._drained.add(channel)
            self._cond.notify_all()

        self._close_finished_channels()

    def _close_finished_channels(self) -> None:
        """Закрыть входы читателей опустевших каналов: при успехе — когда
        писатель закончил успешно, при срыве — когда все вызовы добиты."""
        closing: list[ChannelFanOut] = []

        with self._cond:
            for channel in self._drained:
                if channel in self._closed:
                    continue

                if not self._may_close(channel):
                    continue

                self._closed.add(channel)
                closing.append(self._fanouts[channel])

            self._cond.notify_all()

        for fanout in closing:
            for fd in fanout.reader_fds:
                with suppress(OSError):
                    os.close(fd)

        if closing:
            with self._cond:
                self._cond.notify_all()

    def _may_close(self, channel: str) -> bool:
        if self._failure:
            return self._aborted

        writer = self._runs[self._plan.route(channel).writer]
        return writer.state is NodeState.DONE

    def _at_gate(self, key: str) -> None:
        with self._cond:
            run = self._runs[key]
            if not run.state.terminal():
                run.state = NodeState.AT_GATE

            refused = bool(self._failure)
            call = run.call

        if refused:
            if call is not None:
                call.gate().refuse()
            return

        self._maybe_release()

    def _maybe_release(self) -> None:
        gated: list[ToolCall] = []

        with self._cond:
            if self._failure:
                return

            if self._released:
                return

            for run in self._runs.values():
                if run.state not in (NodeState.AT_GATE, NodeState.DONE):
                    return

            self._released = True
            for run in self._runs.values():
                if run.state is not NodeState.AT_GATE:
                    continue

                if run.call is not None:
                    gated.append(run.call)

        for call in gated:
            call.gate().release()

    def _fail(self, cause: str, *, stopped: bool) -> None:
        """Первый сбой срывает группу: отказ барьерам, добивание живых вызовов,
        затем закрытие каналов."""
        with self._cond:
            if self._failure:
                return

            self._failure = cause
            self._stopped = stopped
            victims: list[ToolCall] = []
            for run in self._runs.values():
                if run.state is NodeState.PENDING:
                    run.state = NodeState.FAILED
                    continue

                if run.state.terminal():
                    continue

                if run.call is not None:
                    self._victims.add(run.node.key)
                    victims.append(run.call)

            self._cond.notify_all()

        logger.warning("stream group %s failed: %s", self.labels(), cause)

        for call in victims:
            call.gate().refuse()

        for call in victims:
            call.close()

        with self._cond:
            self._aborted = True
            unwired = not self._wired
            self._cond.notify_all()

        if unwired:
            self._close_unwired()

        self._close_finished_channels()

    def _close_unwired(self) -> None:
        """Группа сорвалась до раздачи: отданные дескрипторы никому не нужны."""
        with self._cond:
            fds: list[int] = []
            for run in self._runs.values():
                if run.frames_fd >= 0:
                    fds.append(run.frames_fd)
                    run.frames_fd = -1

                fds.extend(run.input_fds)
                run.input_fds = ()

            self._cond.notify_all()

        for fd in fds:
            with suppress(OSError):
                os.close(fd)

    def _discard(
        self, call: ToolCall, outputs: Sequence[int], inputs: Sequence[int]
    ) -> None:
        """Вызов открылся после срыва группы: добить и отпустить каналы."""
        call.gate().refuse()
        call.close()

        for fd in (*outputs, *inputs):
            with suppress(OSError):
                os.close(fd)

    def _settled(self) -> bool:
        for run in self._runs.values():
            if not run.state.terminal():
                return False

        return len(self._closed) == len(self._fanouts)

    def _coordinate(self) -> None:
        last_moved = -1
        last_move_at = time.monotonic()

        while True:
            with self._cond:
                if self._settled():
                    return

                self._cond.wait(timeout=self._timings.poll_sec)
                wired = self._wired
                failed = bool(self._failure)

            if failed:
                continue

            now = time.monotonic()
            if not wired:
                self._check_open(now)
                continue

            moved = self._moved_total()
            if moved != last_moved:
                last_moved = moved
                last_move_at = now
                continue

            if not self._flowing():
                last_move_at = now
                continue

            if now - last_move_at >= self._timings.stall_sec:
                self._fail(self._stall_text(), stopped=False)

    def _check_open(self, now: float) -> None:
        if now - self._opened_at < self._timings.open_sec:
            return

        pending: list[str] = []
        with self._cond:
            for run in self._runs.values():
                if run.state is NodeState.PENDING:
                    pending.append(run.node.label())

        if not pending:
            return

        self._fail(
            f"calls {pending} did not open their channels within "
            f"{self._timings.open_sec:.0f}s",
            stopped=False,
        )

    def _moved_total(self) -> int:
        total = 0
        for fanout in self._fanouts.values():
            total += fanout.moved()

        return total

    def _flowing(self) -> bool:
        """Есть канал, раздача которого ещё не опустела."""
        with self._cond:
            return len(self._drained) < len(self._fanouts)

    def _stall_text(self) -> str:
        open_channels: list[str] = []
        unread: list[str] = []

        with self._cond:
            for route in self._plan.routes():
                if route.name in self._drained:
                    continue

                open_channels.append(route.name)
                unread.extend(self._unread_of(route))

        return (
            f"stream group stalled: no bytes moved on channels {open_channels} "
            f"for {self._timings.stall_sec:.0f}s; readers holding unread input: "
            f"{unread}"
        )

    def _unread_of(self, route: ChannelRoute) -> Iterator[str]:
        fanout = self._fanouts[route.name]
        for reader, fd in zip(route.readers, fanout.reader_fds, strict=True):
            pending = self._pending_bytes(fd)
            if pending == 0:
                continue

            node = self._runs[reader.node].node
            yield f"{node.label()} input #{reader.index} ({pending} bytes)"

    @staticmethod
    def _pending_bytes(fd: int) -> int:
        """Сколько байт лежит в пайпе непрочитанными (FIONREAD)."""
        raw = bytearray(struct.calcsize("i"))
        try:
            fcntl.ioctl(fd, termios.FIONREAD, raw)
        except OSError:
            return 0

        return struct.unpack("i", raw)[0]


class NodeSlot:
    """Ручка одного вызова группы для обёртки запуска (ToolProcessWrap).

    Обёртка берёт её из PipelineSlot, открывает вызов потоково и отдаёт
    группе через attach сам вызов, дескриптор выхода (open_tap) и
    дескрипторы входов по порядку ToolCommand.inputs. counts — сколько
    каналов у каждого входного порта вызова, 0 — вход пуст. По концу
    вызова settle сообщает группе итог и ждёт её решения: вызов отдаёт свой
    итог, только если группа прошла целиком, иначе — отказ с текстом срыва
    группы.
    """

    def __init__(
        self,
        group: StreamGroupRun,
        key: str,
        counts: Mapping[str, int],
        *,
        has_downstream: bool,
    ) -> None:
        self._group = group
        self._key = key
        self._counts = dict(counts)
        self.has_downstream = has_downstream

    def input_counts(self) -> dict[str, int]:
        """Сколько входов у каждого входного порта вызова."""
        return dict(self._counts)

    def attach(
        self, call: ToolCall, outputs: Sequence[int], inputs: Sequence[int]
    ) -> None:
        self._group.attach(self._key, call, outputs, inputs)

    def settle(self, outcome: ToolOutcome) -> ToolOutcome:
        """Итог вызова в группу; ответ — свой итог либо срыв группы."""
        reply = outcome.reply

        cause = ""
        if isinstance(reply, ReplyError):
            cause = f"{reply.kind}: {reply.message}"

        self._group.done(
            self._key, ok=isinstance(reply, ReplyOk), cause=cause, stopped=False
        )
        verdict = self._group.verdict()

        if verdict.ok:
            return outcome

        failed = ReplyError(
            kind=StreamFailureKind.GROUP_FAILED, message=verdict.message
        )
        return ToolOutcome(reply=failed, run=outcome.run, diagnostic=outcome.diagnostic)

    def settle_error(self, error: BaseException) -> GroupVerdict:
        """Вызов сорвался исключением; ответ — решение группы. Остановка хода
        (не Exception), пришедшая не от самой группы, помечает группу как
        остановленную."""
        stopped = not isinstance(error, Exception)
        if self._group.stopped_by_group(self._key):
            stopped = False

        cause = str(error)
        if not cause:
            cause = type(error).__name__

        self._group.done(self._key, ok=False, cause=cause, stopped=stopped)
        return self._group.verdict()


class PipelineSlot:
    """Contextvar-переноска ручки вызова в группе: владелец группы ставит её
    перед вызовом инструмента, обёртка запуска читает в потоке тела. Вне
    группы ручки нет, и вызов идёт обычным накопительным путём."""

    _SLOT: ClassVar[ContextVar[NodeSlot | None]] = ContextVar(
        "boba_pipeline_slot", default=None
    )

    @classmethod
    def set(cls, slot: NodeSlot) -> Token[NodeSlot | None]:
        return cls._SLOT.set(slot)

    @classmethod
    def reset(cls, token: Token[NodeSlot | None]) -> None:
        cls._SLOT.reset(token)

    @classmethod
    def get(cls) -> NodeSlot | None:
        return cls._SLOT.get()
