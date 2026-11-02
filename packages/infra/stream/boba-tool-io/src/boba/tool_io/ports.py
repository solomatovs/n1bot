"""Каналы прогресса и барьера для гостевой программы инструмента.

Ошибки:
OSError — канал закрыт или недоступен.
StreamGroupAbortedError — группа отказала в фиксации результата.
"""

from __future__ import annotations

import os

from boba.toolkit.ports import GateSignal, StreamGroup, ToolProgress

__all__ = ["PipeProgress", "PipeStreamGroup"]


class PipeProgress(ToolProgress):
    """Отправляет отчёты ToolProgress в пайп, заданный ToolMain."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        sender = None
        if fd >= 0:
            sender = self._write

        super().__init__(sender)

    def _write(self, line: bytes) -> None:
        os.write(self._fd, line)


class PipeStreamGroup(StreamGroup):
    """Обменивается решением барьера по двум пайпам, заданным ToolMain."""

    def __init__(self, gate_fd: int, verdict_fd: int) -> None:
        self._gate_fd = gate_fd
        self._verdict_fd = verdict_fd
        receiver = None
        if gate_fd >= 0:
            receiver = self._receive

        super().__init__(receiver)

    def _receive(self) -> GateSignal:
        os.write(self._gate_fd, GateSignal.READY.bytes())

        return GateSignal.of_byte(os.read(self._verdict_fd, 1))
