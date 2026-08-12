"""Показ продукта узла в UI: способ показа, тип файла и реестр инструментов.

Способ показа решает объявленный формат продукта узла: панель берёт его из
журнала стадии, потому что id узла графа приходит из спеки вызывающего и
реестру имён неизвестен. Реестр здесь только один — инструменты, чьи вызовы
идут стадиями песочницы: у их шагов есть кнопка вывода.

Ошибки: наружу не выходят — узел без объявленного продукта показывается общим
выводом стадии.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import ClassVar

from boba.toolkit.channels import Channel, StreamFormat

__all__ = ["StageTools", "StageView"]


class StageView(StrEnum):
    """Как панель показывает продукт узла: канал журнала и способ показа."""

    PAYLOAD = "payload"
    """Текстовый продукт: журнал канала данных."""
    STDOUT = "stdout"
    """Продукта нет: общий вывод стадии."""
    DOWNLOAD = "download"
    """Бинарный продукт: показывать нечем, файл отдаётся скачиванием."""

    @property
    def channel(self) -> Channel:
        """Канал журнала, который открывает панель."""
        if self is StageView.STDOUT:
            return Channel.TOOL_STDOUT

        return Channel.TOOL_PAYLOAD

    @classmethod
    def of(cls, out: StreamFormat | None) -> StageView:
        """Объявленный формат продукта узла -> способ показа."""
        if out is None:
            return cls.STDOUT

        if out is StreamFormat.BYTES:
            return cls.DOWNLOAD

        return cls.PAYLOAD

    @classmethod
    def mime_of(cls, channel: Channel, out: StreamFormat | None) -> str:
        """Тип файла канала: продукт узла отдаётся объявленным форматом.

        Формат объявлен только у канала данных; остальные каналы стадии —
        человеческий текст независимо от продукта. Кодировку текстовых типов
        добавляет отдача файла.
        """
        if channel is not Channel.TOOL_PAYLOAD:
            return StreamFormat.TEXT.value

        if out is None:
            return StreamFormat.TEXT.value

        return out.value


class StageTools:
    """Инструменты, чьи вызовы идут стадиями песочницы: у их шагов есть журнал."""

    _TOOLS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def configure(cls, tools: Iterable[str]) -> None:
        cls._TOOLS = frozenset(tools)

    @classmethod
    def journalled(cls, tool_name: str) -> bool:
        """Вызов инструмента идёт стадиями песочницы: у его шага есть журнал."""
        return tool_name in cls._TOOLS

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        cls._TOOLS = frozenset()
