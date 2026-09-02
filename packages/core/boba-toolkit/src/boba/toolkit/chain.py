"""Перекачка потока между вызовами инструментов: проверка стыковки и релей.

Цепочка A -> B — это выходной канал кадров вызова A, направленный во вход
вызова B. Здесь живёт весь механизм: ChainCheck сверяет декларации портов
(StreamSpec) до запуска, CallRelay переливает данные. Путей перекачки два:

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
from contextlib import suppress
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.toolkit.launcher import LauncherError, ToolCall
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpec
from boba.toolkit.pump import OpenRun

__all__ = [
    "CallRelay",
    "ChainCheck",
    "ChainMismatchError",
    "RelayStats",
    "TappedCall",
]


class ChainMismatchError(LauncherError):
    """Выход source не подходит входу sink: цепочку собирать нельзя."""


@dataclass(frozen=True)
class TappedCall:
    """Вызов-источник для splice-перекачки: сам вызов и дескриптор его
    канала кадров, который хост не разбирает. Возвращается методом
    open_tap реализаций ToolLauncher; дескриптором владеет перекачка."""

    call: ToolCall
    frames_fd: int


class RelayStats(BaseModel):
    """Итог перекачки: сколько прошло. У splice-пути кадры не считаются —
    хост их не разбирает, есть только байты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frames: int
    bytes: int
    spliced: bool


class ChainCheck:
    """Сверка деклараций портов цепочки до запуска.

    Правила: у source обязан быть выходной порт, у sink — входной; сырой
    канал совместим только с сырым (модельный поток кадрирован — его рамки
    попали бы в данные сырого входа, а сырому потоку нечем пройти модельную
    валидацию); модельные порты совместимы, когда каждый kind выхода
    объявлен на входе.
    """

    @classmethod
    def ensure(cls, source: StreamSpec, sink: StreamSpec) -> None:
        outbound = cls._port(source, PortDirection.OUTBOUND, "source")
        inbound = cls._port(sink, PortDirection.INBOUND, "sink")

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

    @staticmethod
    def _port(spec: StreamSpec, direction: PortDirection, side: str) -> PortDecl:
        for port in spec.ports:
            if port.direction is direction:
                return port

        msg = f"{side} declares no {direction} port"
        raise ChainMismatchError(msg)


class CallRelay:
    """Перекачка данных из открытого вызова-источника в вызов-приёмник."""

    SPLICE_BYTES: ClassVar[int] = 1 << 20

    @staticmethod
    def frames(source: ToolCall, sink: ToolCall) -> RelayStats:
        """Универсальная перекачка кадрами через хост.

        Читает кадры source до конца его вызова, шлёт их в sink и закрывает
        его вход. Итоги вызовов остаются вызывающему: result() обеих сторон
        он читает сам.
        """
        count = 0
        size = 0

        for frame in source.frames():
            sink.send(frame)
            count += 1
            size += len(frame.body)

        sink.done_sending()

        return RelayStats(frames=count, bytes=size, spliced=False)

    @staticmethod
    def input_fd(sink: ToolCall) -> int:
        """Дескриптор входа приёмника для splice-перекачки.

        Забирает вход у открытого вызова (CallInput.take_fd): send и
        done_sending на нём после этого не работают — входом владеет
        перекачка. Вызов обязан быть прогоном OpenRun (PumpedCall).
        """
        if not isinstance(sink, OpenRun):
            msg = "sink call does not expose its input descriptor"
            raise LauncherError(msg)

        return sink.entry.take_fd()

    @classmethod
    def splice(cls, source_fd: int, sink_fd: int) -> RelayStats:
        """Zero-copy перекачка пайп -> пайп силами ядра.

        Дескрипторы приходят из TappedCall (open_tap источника) и
        input_fd() приёмника; оба закрываются здесь на любом исходе —
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
