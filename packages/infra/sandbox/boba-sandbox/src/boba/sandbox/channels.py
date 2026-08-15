"""Дескрипторы одного запуска инструмента: пайпы, раздача ребёнку, приёмники.

Ошибки: ChannelError — канал не открыть или он уже закрыт.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from typing import ClassVar, Self

from boba.toolkit.channels import ToolChannel

__all__ = ["ChannelError", "ChannelSet", "TailSink"]


class ChannelError(Exception):
    """Канал не открыть или набор уже закрыт."""


class TailSink:
    """Хвост канала в памяти: объяснение падения, когда конверта нет."""

    DEFAULT_BYTES: ClassVar[int] = 4096

    def __init__(self, max_bytes: int = DEFAULT_BYTES) -> None:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive, got {max_bytes}"
            raise ValueError(msg)

        self._max = max_bytes
        self._tail = bytearray()

    def feed(self, data: bytes) -> None:
        self._tail.extend(data)

        if len(self._tail) > self._max:
            del self._tail[: len(self._tail) - self._max]

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")


class ChannelSet:
    """Пайпы исходящих каналов: создать, раздать номера ребёнку, закрыть.

    Ребёнок наследует write-концы как есть (`pass_fds` сохраняет номера),
    родитель читает read-концы насосом. STDIN пайпом не является — его несёт
    stdin процесса.
    """

    def __init__(self, pipes: Mapping[ToolChannel, tuple[int, int]]) -> None:
        self._pipes = dict(pipes)
        self._sinks: dict[ToolChannel, Callable[[bytes], None]] = {}
        self._child_closed = False
        self._closed = False

    @classmethod
    def open(cls, channels: Iterable[ToolChannel]) -> Self:
        pipes: dict[ToolChannel, tuple[int, int]] = {}

        try:
            for channel in channels:
                if channel.inbound:
                    continue

                read_fd, write_fd = os.pipe()
                os.set_inheritable(write_fd, True)
                pipes[channel] = (read_fd, write_fd)
        except OSError as exc:
            for read_fd, write_fd in pipes.values():
                os.close(read_fd)
                os.close(write_fd)
            raise ChannelError(f"cannot open channel pipes: {exc}") from exc

        return cls(pipes)

    @property
    def child_fds(self) -> Mapping[ToolChannel, int]:
        """Номера write-концов, которые ребёнок унаследует как есть."""
        fds: dict[ToolChannel, int] = {}
        for channel, (_read_fd, write_fd) in self._pipes.items():
            fds[channel] = write_fd

        return fds

    @property
    def parent_fds(self) -> Mapping[ToolChannel, int]:
        """Номера read-концов на стороне родителя."""
        fds: dict[ToolChannel, int] = {}
        for channel, (read_fd, _write_fd) in self._pipes.items():
            fds[channel] = read_fd

        return fds

    def env(self) -> Mapping[str, str]:
        """Переменные с номерами fd для каналов, читаемых инструментом из env."""
        variables: dict[str, str] = {}
        if ToolChannel.RESULT in self._pipes:
            fd = self._pipes[ToolChannel.RESULT][1]
            variables[ToolChannel.RESULT.env_name] = str(fd)

        return variables

    def redirect(self) -> str:
        """Преамбула 'exec 1>&N 2>&M': fd 1/2 тела — каналы STDOUT/STDERR."""
        parts: list[str] = []
        if ToolChannel.STDOUT in self._pipes:
            parts.append(f"1>&{self._pipes[ToolChannel.STDOUT][1]}")

        if ToolChannel.STDERR in self._pipes:
            parts.append(f"2>&{self._pipes[ToolChannel.STDERR][1]}")

        if not parts:
            return ":"

        return "exec " + " ".join(parts)

    def add_sink(self, channel: ToolChannel, sink: Callable[[bytes], None]) -> None:
        """Дополнительный приёмник канала; ставится тройником к уже стоящим."""
        if channel not in self._pipes:
            msg = f"channel {channel} is not open"
            raise ChannelError(msg)

        current = self._sinks.get(channel)
        if current is None:
            self._sinks[channel] = sink
            return

        def tee(data: bytes, first: Callable[[bytes], None] = current) -> None:
            first(data)
            sink(data)

        self._sinks[channel] = tee

    def sinks(self) -> Mapping[int, Callable[[bytes], None]]:
        """Карта read-fd -> приёмник; по ней читает насос run_subprocess."""
        table: dict[int, Callable[[bytes], None]] = {}
        for channel, (read_fd, _write_fd) in self._pipes.items():
            sink = self._sinks.get(channel)
            if sink is None:
                continue

            table[read_fd] = sink

        # каналы без приёмника всё равно читаются — иначе тело блокируется
        for read_fd, _write_fd in self._pipes.values():
            if read_fd not in table:
                table[read_fd] = self._discard

        return table

    @staticmethod
    def _discard(_data: bytes) -> None:
        """Приёмник канала без потребителя: прочитать и выбросить."""

    def close_child_ends(self) -> None:
        """Родительские копии write-концов; без этого пайп не даст EOF."""
        if self._child_closed:
            return

        self._child_closed = True
        for _read_fd, write_fd in self._pipes.values():
            os.close(write_fd)

    def close(self) -> None:
        """Read-концы после насоса; повторное закрытие ничего не делает."""
        if self._closed:
            return

        self._closed = True
        self.close_child_ends()
        for read_fd, _write_fd in self._pipes.values():
            os.close(read_fd)


def _discard(_data: bytes) -> None:
    """Приёмник канала без потребителя: прочитать и выбросить."""
