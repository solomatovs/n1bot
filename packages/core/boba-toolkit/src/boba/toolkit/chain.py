"""Перекачка потока между вызовами инструментов: проверка стыковки и релей.

Цепочка A -> B — это выходной канал кадров вызова A, направленный во вход
вызова B. Здесь живёт весь механизм: ChainCheck сверяет стыкуемые порты
(PortDecl) до запуска, CallRelay переливает данные. Путей перекачки два:

- frames() — универсальный, через хост: кадры читаются из source и шлются
  в sink; работает с любыми ToolCall (в том числе между разными
  лончерами и секциями), хост видит каждый кадр.
- splice() — zero-copy, через ядро: source открывается методом open_tap
  реализации лончера (канал кадров отдаётся дескриптором и хостом не
  разбирается), у sink дескриптор входа забирается take_fd; ядро
  переливает пайп в пайп, данные в userspace не поднимаются. Кадры
  пролетают как байты и раскодируются только на стороне B.

Backpressure сквозной на обоих путях: медленный приёмник останавливает
источник через полные буферы пайпов.

Ошибки:
ChainMismatchError — декларации портов source и sink несовместимы.
LauncherError — вызов нарушил контракт; поднимают send/frames вызовов.
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from contextvars import ContextVar, Token
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict

from boba.toolkit.launcher import CallInputPort, LauncherError, TappedCall, ToolCall
from boba.toolkit.ports import PortDecl, PortDirection

__all__ = [
    "CallRelay",
    "ChainCheck",
    "ChainMismatchError",
    "NodeSlot",
    "PipelineSlot",
    "RelayStats",
    "TappedCall",
]


ValueT = TypeVar("ValueT")


class ChainMismatchError(LauncherError):
    """Выход source не подходит входу sink: цепочку собирать нельзя."""


class NodeSlot:
    """Роль одного вызова в конвейере: какие его каналы отданы рёбрам.

    Оркестратор создаёт слот на узел и публикует его через PipelineSlot
    перед вызовом инструмента; обёртка запуска (ToolProcessWrap), увидев
    слот, открывает потоковый вызов вместо накопительного и отдаёт сюда
    дескрипторы каналов. inputs — входные порты вызова по одному на вход,
    в порядке входов: порт-список повторяется столько раз, сколько у него
    входов. Оркестратор забирает дескрипторы из своего потока и соединяет
    их перекачкой; abort() добивает вызов узла при сбое конвейера.
    """

    def __init__(self, *, inputs: Sequence[str], has_downstream: bool) -> None:
        self.inputs = tuple(inputs)
        self.has_downstream = has_downstream
        self._source_fd: queue.Queue[int] = queue.Queue(maxsize=1)
        self._input_fds: queue.Queue[tuple[int, ...]] = queue.Queue(maxsize=1)
        self._abort_lock = threading.Lock()
        self._abort: Callable[[], None] | None = None
        self._aborted = False

    @property
    def has_upstream(self) -> bool:
        return bool(self.inputs)

    def input_counts(self) -> dict[str, int]:
        """Сколько входов у каждого входного порта вызова."""
        counts: dict[str, int] = {}
        for port in self.inputs:
            counts[port] = counts.get(port, 0) + 1

        return counts

    def give_source_fd(self, fd: int) -> None:
        """Обёртка отдаёт дескриптор выходного канала узла (open_tap)."""
        self._source_fd.put_nowait(fd)

    def give_input_fds(self, fds: Sequence[int]) -> None:
        """Обёртка отдаёт дескрипторы входов узла в порядке inputs."""
        self._input_fds.put_nowait(tuple(fds))

    def take_source_fd(self, timeout_sec: float) -> int:
        return self._take(self._source_fd, timeout_sec, "source")

    def take_input_fds(self, timeout_sec: float) -> tuple[int, ...]:
        return self._take(self._input_fds, timeout_sec, "input")

    def attach_abort(self, abort: Callable[[], None]) -> None:
        """Обёртка регистрирует добивание своего вызова; при уже сорванном
        конвейере оно исполняется немедленно."""
        with self._abort_lock:
            self._abort = abort
            fire = self._aborted

        if fire:
            abort()

    def abort(self) -> None:
        """Сорвать узел: добить его вызов и закрыть неразобранные каналы."""
        with self._abort_lock:
            self._aborted = True
            abort = self._abort

        if abort is not None:
            abort()

        self._drain()

    def _take(self, box: queue.Queue[ValueT], timeout_sec: float, side: str) -> ValueT:
        try:
            return box.get(timeout=timeout_sec)
        except queue.Empty:
            msg = (
                f"pipeline node gave no {side} descriptor within "
                f"{timeout_sec:.0f}s: the call did not open its channel"
            )
            raise ChainMismatchError(msg) from None

    def _drain(self) -> None:
        while True:
            try:
                fd = self._source_fd.get_nowait()
            except queue.Empty:
                break

            with suppress(OSError):
                os.close(fd)

        while True:
            try:
                fds = self._input_fds.get_nowait()
            except queue.Empty:
                break

            for fd in fds:
                with suppress(OSError):
                    os.close(fd)


class PipelineSlot:
    """Contextvar-переноска слота конвейера: оркестратор ставит слот перед
    вызовом узла, обёртка запуска читает его в потоке тела. Вне конвейера
    слот пуст, и вызов идёт обычным накопительным путём."""

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


class RelayStats(BaseModel):
    """Итог перекачки: сколько прошло. У splice-пути кадры не считаются —
    хост их не разбирает, есть только байты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frames: int
    bytes: int
    spliced: bool


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


class CallRelay:
    """Перекачка данных из открытого вызова-источника в вызов-приёмник."""

    SPLICE_BYTES: ClassVar[int] = 1 << 20

    @staticmethod
    def frames(source: ToolCall, sink: CallInputPort) -> RelayStats:
        """Универсальная перекачка кадрами через хост.

        Читает кадры source до конца его вызова, шлёт их во вход приёмника и
        закрывает его. Итоги вызовов остаются вызывающему: result() обеих
        сторон он читает сам.
        """
        count = 0
        size = 0

        for frame in source.frames():
            sink.send(frame)
            count += 1
            size += len(frame.body)

        sink.finish()

        return RelayStats(frames=count, bytes=size, spliced=False)

    @classmethod
    def splice(cls, source_fd: int, sink_fd: int) -> RelayStats:
        """Zero-copy перекачка пайп -> пайп силами ядра.

        Дескрипторы приходят из TappedCall (open_tap источника) и take_fd()
        входа приёмника; оба закрываются здесь на любом исходе —
        закрытие входа и есть EOF для тела приёмника. Вызов блокирует до
        конца потока, поэтому запускается до закачки входа источника либо
        своим потоком — иначе вызывающий заблокирует сам себя на полных
        буферах пайпов.

        Смерть приёмника посреди перекачки не глотается: перекачка
        останавливается, источник получает EPIPE на своём канале (как в
        shell-конвейере), причины видны в result() обеих сторон.
        """
        total = 0

        try:
            while True:
                try:
                    moved = os.splice(source_fd, sink_fd, cls.SPLICE_BYTES)
                except BrokenPipeError:
                    break

                if moved == 0:
                    break

                total += moved
        finally:
            with suppress(OSError):
                os.close(source_fd)

            with suppress(OSError):
                os.close(sink_fd)

        return RelayStats(frames=0, bytes=total, spliced=True)
