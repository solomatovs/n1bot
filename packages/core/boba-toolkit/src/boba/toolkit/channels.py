"""Имена каналов одного вызова инструмента.

Процесс инструмента общается с приложением несколькими потоками байтов:
обычные stdin/stdout/stderr, конверт результата и кадры данных. Этот модуль
даёт каждому потоку имя и определяет, какие из них можно показывать
пользователю, а какие служебные. Именами каналов названы файлы журнала
вызова, по ним же панель запрашивает чтение.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

__all__ = [
    "CallOutcome",
    "JournalChannel",
    "JournalChannels",
    "ToolChannel",
    "WrapChannel",
]


class CallOutcome(StrEnum):
    """Чем закончился вызов — пометка, которой закрывается его журнал.

    Пишет её обвязка запуска (run_log — исходом вызова, конец хода закрывает
    брошенные журналы как STOPPED), а панель показывает как статус потока.
    Словарь общий для писателя и читателя, чтобы статусы не разъехались.
    """

    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class ToolChannel(StrEnum):
    """Потоки байтов самого тела инструмента; значение — имя файла в журнале.

    STDOUT и STDERR — обычный вывод процесса (логи, печать), их видит
    пользователь. STDIN несёт кадры входных данных до EOF, FRAMES — кадры
    данных наружу, RESULT — конверт результата (см. boba.toolkit.frames и
    boba.toolkit.protocol); эти три — служебные. Номера дескрипторов RESULT
    и FRAMES лончер передаёт телу аргументами команды (флаги EntryFlag в
    boba.toolkit.entry).
    """

    STDIN = "tool_stdin"
    STDOUT = "tool_stdout"
    STDERR = "tool_stderr"
    RESULT = "tool_result"
    FRAMES = "tool_frames"

    @property
    def inbound(self) -> bool:
        """Канал ведёт в процесс инструмента, а не из него."""
        return self is ToolChannel.STDIN


class WrapChannel(StrEnum):
    """Вывод обвязки запуска — процессов песочницы вокруг тела (bwrap,
    лаунчер образов).

    Отделён от каналов тела (ToolChannel), чтобы шум монтирования и
    изоляции не смешивался с выводом самого инструмента: в панель обвязка
    не попадает, но в журнале сохраняется для разбора сбоев.
    """

    STDOUT = "wrap_stdout"
    STDERR = "wrap_stderr"


JournalChannel = ToolChannel | WrapChannel
"""Канал журнала вызова: тело инструмента либо обвязка запуска."""


class JournalChannels:
    """Реестр каналов журнала вызова: полный перечень, разбор имени и
    правило видимости.

    Журнал пишет все каналы, но пользователю — в панель, окна чтения и
    скачивание — отдаются только stdout и stderr тела. Конверт, кадры,
    stdin и вывод обвязки остаются служебными: их читает разбор сбоев на
    сервере. Правило видимости одно на все точки входа, чтобы права
    доступа не разъехались.
    """

    VISIBLE: ClassVar[tuple[JournalChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.STDERR,
    )
    """Каналы, доступные пользователю; порядок задаёт вкладки панели."""

    @classmethod
    def order(cls) -> tuple[JournalChannel, ...]:
        """Все каналы журнала: сначала тело, потом обвязка."""
        channels: list[JournalChannel] = []

        for tool_channel in ToolChannel:
            channels.append(tool_channel)

        for wrap_channel in WrapChannel:
            channels.append(wrap_channel)

        return tuple(channels)

    @classmethod
    def parse(cls, raw: str) -> JournalChannel | None:
        """Канал по имени; None — имя не принадлежит ни одному каналу."""
        for channel in cls.order():
            if channel.value == raw:
                return channel

        return None

    @classmethod
    def visible(cls, channel: JournalChannel) -> bool:
        """Разрешён ли канал к чтению пользователем."""
        return channel in cls.VISIBLE

    @classmethod
    def parse_visible(cls, raw: str) -> JournalChannel | None:
        """Канал по имени с проверкой доступа; None — читать его нельзя."""
        channel = cls.parse(raw)
        if channel is None:
            return None

        if not cls.visible(channel):
            return None

        return channel
