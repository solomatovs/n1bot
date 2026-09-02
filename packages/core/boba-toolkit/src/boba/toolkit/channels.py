"""Каналы одного вызова инструмента: имена, направление, env-реестр.

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
    """Пометка закрытия журнала вызова: её пишет обвязка, читает панель.

    Один словарь на писателя и читателя: run_log закрывает журнал исходом
    вызова, конец хода закрывает брошенные журналы STOPPED, панель
    показывает пометку как статус потока.
    """

    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class ToolChannel(StrEnum):
    """Каналы тела инструмента; значение — имя канала в файле журнала.

    Вход и кадры наружу кадрированы (boba.toolkit.frames): stdin несёт
    config, прикладные кадры и eos, frames — кадры тела.
    """

    STDIN = "tool_stdin"
    STDOUT = "tool_stdout"
    STDERR = "tool_stderr"
    RESULT = "tool_result"
    FRAMES = "tool_frames"

    @property
    def env_name(self) -> str:
        """Имя env-переменной с номером fd: BOBA_FD_RESULT, BOBA_FD_FRAMES."""
        return f"BOBA_FD_{self.name}"

    @property
    def inbound(self) -> bool:
        """Канал ведёт в процесс инструмента, а не из него."""
        return self is ToolChannel.STDIN


class WrapChannel(StrEnum):
    """Каналы обвязки запуска: stdout/stderr самого процесса песочницы.

    Тело инструмента пишет в свои дескрипторы (ToolChannel), поэтому здесь
    остаётся вывод обвязки — лаунчера образов и bwrap.
    """

    STDOUT = "wrap_stdout"
    STDERR = "wrap_stderr"


JournalChannel = ToolChannel | WrapChannel
"""Канал журнала вызова: тело инструмента либо обвязка запуска."""


class JournalChannels:
    """Каналы журнала: что бывает, что видно пользователю и разбор имени.

    Пишутся все каналы, но наружу — в панель, окна чтения и скачивание —
    отдаются только stdout и stderr тела инструмента. Остальное служебное:
    конверт результата, стдин и вывод обвязки запуска живут в журнале для
    разбора сбоев, а не для чтения из чата. Список один на все точки входа.
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
