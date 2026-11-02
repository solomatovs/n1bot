"""Исполнение группы инструментов через Linux-пайпы и общий барьер.

Ошибки:
LauncherError — исполнитель нарушил контракт запуска.
OSError — операция над пайпом или дескриптором не выполнена.
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
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from enum import StrEnum
from typing import ClassVar

from boba.tool_io.pump import PipePlumbing
from boba.toolkit.chain import (
    ChannelRoute,
    GroupCall,
    GroupFailureResult,
    GroupVerdict,
    StreamFailureKind,
    StreamNode,
    StreamPlan,
    StreamTimings,
)
from boba.toolkit.failure import FailurePacker
from boba.toolkit.launcher import (
    LauncherError,
    ToolCall,
    ToolOutcome,
)
from boba.toolkit.protocol import (
    CallInputSpec,
    CallOutputSpec,
    ReplyError,
    ToolCommand,
)
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
)
from boba.toolkit.stream import ChannelSinks

__all__ = ["CallAmbient", "ChannelFanOut", "NodeSlot", "PipeTee", "StreamGroupRun"]

logger = logging.getLogger(__name__)


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
        pipe_bytes: int = 0,
    ) -> None:
        self._channel = channel
        self._source_fd = source_fd
        self._reader_fds = tuple(reader_fds)
        self._tee = tee
        self._on_error = on_error
        self._on_drained = on_drained
        self._pipe_bytes = pipe_bytes
        self._lock = threading.Lock()
        self._moved = [0] * len(self._reader_fds)
        self._running = len(self._reader_fds)
        self._links: list[int] = []

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
        """Запустить звенья раздачи.

        Ошибки:
        LauncherError — ядро не дало буфер промежуточному пайпу: ни одно
            звено не стартует, вход писателя и созданные пайпы закрыты,
            on_drained не придёт.
        """
        try:
            hops = list(self._hops())
        except LauncherError:
            self._abandon()
            raise

        for hop in hops:
            hop.start()

    def _abandon(self) -> None:
        """Раздача не стартовала: закрыть вход писателя и промежуточные пайпы."""
        for fd in (self._source_fd, *self._links):
            with suppress(OSError):
                os.close(fd)

        self._links = []

    def _hops(self) -> Iterator[threading.Thread]:
        last = len(self._reader_fds) - 1
        inbound = self._source_fd

        for index, reader_fd in enumerate(self._reader_fds):
            if index == last:
                yield self._thread(index, self._last_hop, inbound, reader_fd, -1)
                return

            next_r, next_w = os.pipe()
            self._links.extend((next_r, next_w))
            PipePlumbing.require(next_w, self._pipe_bytes, self._channel)
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
        self.out_fds: tuple[int, ...] = ()
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
      срыв с его причиной: сорвавший — свою ошибку, остальные — ссылку на
      него.

    Координатор живёт своим потоком: следит за сроком открытия и застоем.
    Группа без каналов — один вызов инструмента без портов — исполняется
    тем же путём, только координатор ей не нужен.
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
        self._failure: FailureResult | None = None
        self._origin = ""
        self._stopped = False
        self._victims: set[str] = set()
        self._aborted = False
        self._opened_at = time.monotonic()

        # координатор следит за сроком открытия каналов и застоем данных:
        # группе без каналов следить не за чем
        if plan.routes():
            threading.Thread(
                target=self._coordinate, name="stream-group", daemon=True
            ).start()

    def slot(self, key: str) -> NodeSlot:
        """Ручка вызова key для обёртки запуска."""
        node = self._plan.node(key)

        in_counts: dict[str, int] = {}
        for port in node.spec.inbound():
            in_counts[port.name] = 0

        for bound in self._plan.inputs_of(key):
            in_counts[bound.port] += 1

        out_counts: dict[str, int] = {}
        for port in node.spec.outbound():
            out_counts[port.name] = 0

        for output in self._plan.outputs_of(key):
            out_counts[output.port] += 1

        in_sizes: list[int] = []
        for bound in self._plan.inputs_of(key):
            in_sizes.append(self._plan.pipe_bytes_of(bound.channel))

        out_sizes: list[int] = []
        for output in self._plan.outputs_of(key):
            out_sizes.append(self._plan.pipe_bytes_of(output.channel))

        return NodeSlot(
            self, key, in_counts, out_counts, tuple(in_sizes), tuple(out_sizes)
        )

    def refuse(self, key: str, cause: FailureResult) -> None:
        """Вызов key не дошёл до запуска (права, аргументы): группа срывается."""
        self.done(key, cause=cause, stopped=False)

    def abort(self, cause: FailureResult) -> None:
        """Сорвать группу снаружи (остановка хода)."""
        self._fail(cause, origin="", stopped=False)

    def solitary(self) -> bool:
        """Группа из одного вызова: её срыв — собственный итог этого вызова."""
        return len(self._runs) == 1

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
            failed = self._failure is not None
            if not failed:
                run.call = call
                run.state = NodeState.OPEN
                run.input_fds = tuple(inputs)
                run.out_fds = tuple(outputs)

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
            self._start_fanout(fanout)

        if fanouts:
            logger.info(
                "stream group %s: wired %d channels", self.labels(), len(fanouts)
            )

    def done(self, key: str, *, cause: FailureResult | None, stopped: bool) -> None:
        """Вызов закончился: успешно (cause=None) либо со сбоем; stopped — его
        остановил ход, а не группа."""
        run = self._runs[key]

        with self._cond:
            if run.state.terminal():
                return

            run.state = NodeState.DONE
            if cause is not None:
                run.state = NodeState.FAILED

            self._cond.notify_all()

        if cause is not None:
            self._fail(cause, origin=key, stopped=stopped)
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
            origin = self._origin
            stopped = self._stopped

        if failure is None:
            return GroupVerdict(failure=None, stopped=False)

        grouped = GroupFailureResult(
            error_kind=StreamFailureKind.GROUP_FAILED,
            cause=failure,
            origin=self._call_of(origin),
            calls=list(self._calls()),
        )

        return GroupVerdict(failure=grouped, stopped=stopped)

    def _call_of(self, key: str) -> GroupCall | None:
        """Вызов, сорвавший группу; None — группу сорвала она сама."""
        run = self._runs.get(key)
        if run is None:
            return None

        return GroupCall(tool=run.node.tool, call_id=run.node.key)

    def _calls(self) -> Iterator[GroupCall]:
        for run in self._runs.values():
            yield GroupCall(tool=run.node.tool, call_id=run.node.key)

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
        source_fd = self._runs[route.writer].out_fds[route.writer_index]

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
            pipe_bytes=self._plan.pipe_bytes_of(channel),
        )

    def _start_fanout(self, fanout: ChannelFanOut) -> None:
        """Запуск раздачи; не стартовала — срыв группы, канал считается
        опустевшим: его звеньев нет, и ждать от него конца нечего."""
        try:
            fanout.start()
        except LauncherError as exc:
            self._fanout_failed(str(exc))
            self._drain(fanout.channel)

    def _fanout_failed(self, cause: str) -> None:
        failure = ErrorResult(
            message=cause, error_kind=StreamFailureKind.CHANNEL_FAILED
        )
        self._fail(failure, origin="", stopped=False)

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
        if self._failure is not None:
            return self._aborted

        writer = self._runs[self._plan.route(channel).writer]
        return writer.state is NodeState.DONE

    def _at_gate(self, key: str) -> None:
        with self._cond:
            run = self._runs[key]
            if not run.state.terminal():
                run.state = NodeState.AT_GATE

            refused = self._failure is not None
            call = run.call

        if refused:
            if call is not None:
                call.gate().refuse()
            return

        self._maybe_release()

    def _maybe_release(self) -> None:
        gated: list[ToolCall] = []

        with self._cond:
            if self._failure is not None:
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

    def _fail(self, cause: FailureResult, *, origin: str, stopped: bool) -> None:
        """Первый сбой срывает группу: отказ барьерам, добивание живых вызовов,
        затем закрытие каналов. origin — ключ сорвавшего вызова, пусто —
        группу сорвала она сама."""
        with self._cond:
            if self._failure is not None:
                return

            self._failure = cause
            self._origin = origin
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

        if stopped:
            logger.info("stream group %s stopped: %s", self.labels(), cause.llm_view())
        else:
            logger.warning(
                "stream group %s failed: %s", self.labels(), cause.log_view()
            )

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
                fds.extend(run.out_fds)
                run.out_fds = ()

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
                failed = self._failure is not None

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
                stalled = ErrorResult(
                    message=self._stall_text(), error_kind=StreamFailureKind.STALLED
                )
                self._fail(stalled, origin="", stopped=False)

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

        message = (
            f"calls {pending} did not open their channels within "
            f"{self._timings.open_sec:.0f}s"
        )
        failure = ErrorResult(message=message, error_kind=StreamFailureKind.NOT_OPENED)
        self._fail(failure, origin="", stopped=False)

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

    Обёртка берёт её из CallAmbient, открывает вызов потоково и отдаёт
    группе через attach сам вызов, дескрипторы выходов (open_tap) и
    дескрипторы входов по порядку ToolCommand.inputs. Счётчики — сколько
    каналов у каждого порта вызова, 0 — канала нет. По концу
    вызова settle сообщает группе итог и ждёт её решения: вызов отдаёт свой
    итог, только если группа прошла целиком, иначе — срыв группы глазами
    этого вызова (GroupFailureResult).
    """

    def __init__(  # noqa: PLR0913 — ручка несёт все стороны одного вызова
        self,
        group: StreamGroupRun,
        key: str,
        in_counts: Mapping[str, int],
        out_counts: Mapping[str, int],
        in_sizes: tuple[int, ...] = (),
        out_sizes: tuple[int, ...] = (),
    ) -> None:
        self._group = group
        self._key = key
        self._in_counts = dict(in_counts)
        self._out_counts = dict(out_counts)
        self._in_sizes = in_sizes
        self._out_sizes = out_sizes
        self._failures = FailurePacker()

    @property
    def key(self) -> str:
        """Ключ вызова в группе: id вызова модели."""
        return self._key

    def input_counts(self) -> dict[str, int]:
        """Сколько каналов у каждого входного порта вызова."""
        return dict(self._in_counts)

    def input_sizes(self) -> tuple[int, ...]:
        """Требуемые буферы пайпов входов по порядку ToolCommand.inputs."""
        return self._in_sizes

    def output_counts(self) -> dict[str, int]:
        """Сколько каналов у каждого выходного порта вызова."""
        return dict(self._out_counts)

    def has_outputs(self) -> bool:
        """Вызов пишет хотя бы один канал: открывать его через open_tap."""
        return any(self._out_counts.values())

    def sized(self, command: ToolCommand) -> ToolCommand:
        """Команда с размерами пайпов каналов из плана группы.

        Размер канала задаёт его писатель, поэтому вход получает размер
        канала, который читает, а выход — размер своего вызова. Порядок
        каналов команды и плана один — порядок портов подписи.
        """
        if not self._in_sizes and not self._out_sizes:
            return command

        inputs: list[CallInputSpec] = []
        for spec, size in zip(command.inputs, self._in_sizes, strict=True):
            inputs.append(spec.model_copy(update={"pipe_bytes": size}))

        outputs: list[CallOutputSpec] = []
        for spec, size in zip(command.outputs, self._out_sizes, strict=True):
            outputs.append(spec.model_copy(update={"pipe_bytes": size}))

        return command.model_copy(
            update={"inputs": tuple(inputs), "outputs": tuple(outputs)}
        )

    def attach(
        self, call: ToolCall, outputs: Sequence[int], inputs: Sequence[int]
    ) -> None:
        self._group.attach(self._key, call, outputs, inputs)

    def solitary(self) -> bool:
        """Вызов — единственный в своей группе."""
        return self._group.solitary()

    def settle(self, outcome: ToolOutcome) -> ToolOutcome:
        """Итог вызова в группу; ответ — свой итог либо срыв группы.

        Единственный вызов группы отвечает своим итогом: делить срыв ему
        не с кем."""
        reply = outcome.reply

        cause: FailureResult | None = None
        if isinstance(reply, ReplyError):
            cause = reply.failure

        self._group.done(self._key, cause=cause, stopped=False)
        verdict = self._group.verdict()

        if verdict.ok:
            return outcome

        if self._group.solitary():
            return outcome

        failed = ReplyError(failure=verdict.failure_of(self._key))
        return ToolOutcome(reply=failed, run=outcome.run, diagnostic=outcome.diagnostic)

    def settle_error(self, error: BaseException) -> GroupVerdict:
        """Вызов сорвался исключением; ответ — решение группы. Остановка хода
        (не Exception), пришедшая не от самой группы, помечает группу как
        остановленную."""
        stopped = not isinstance(error, Exception)
        if self._group.stopped_by_group(self._key):
            stopped = False

        cause = self._failures.pack(error)
        self._group.done(self._key, cause=cause, stopped=stopped)

        return self._group.verdict()


class CallAmbient:
    """Обстановка текущего вызова инструмента для исполнителей: приёмники
    журнала вызова и ручка вызова в группе.

    Объект один на процесс: его создаёт сборка приложения и отдаёт через
    конструкторы тем, кто ставит обстановку (обвязка журнала ToolRunLogger,
    исполнитель DAG) и кто её читает (обёртка запуска, исполнители процесса
    и песочницы, слив dev_null). Значения живут в переменных контекста
    исполнения, поэтому переезжают в поток тела вызова; UI-слой при этом
    исполнителем не импортируется. Вне группы ручки нет, и вызов идёт
    обычным накопительным путём.
    """

    def __init__(self) -> None:
        self._sinks: ContextVar[ChannelSinks | None] = ContextVar(
            "boba_call_sinks", default=None
        )
        self._slot: ContextVar[NodeSlot | None] = ContextVar(
            "boba_call_slot", default=None
        )

    def sinks(self) -> ChannelSinks | None:
        """Приёмники журнала текущего вызова; None — вызов не журналируется."""
        return self._sinks.get()

    def set_sinks(self, sinks: ChannelSinks | None) -> None:
        self._sinks.set(sinks)

    def slot(self) -> NodeSlot | None:
        """Ручка текущего вызова в группе; None — вызов вне группы."""
        return self._slot.get()

    @contextmanager
    def in_slot(self, slot: NodeSlot) -> Generator[None, None, None]:
        """Ставит ручку вызова в группе на время блока."""
        token = self._slot.set(slot)
        try:
            yield
        finally:
            self._slot.reset(token)
