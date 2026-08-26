"""Обёртка запуска: тело инструмента исполняется отдельным процессом.

Ставится первой, на нетронутое тело: захватывает адрес модуля, оригинальную
схему и само тело; вызов уезжает командой модуля инструментов через порт
ToolLauncher.

Ошибки:
PayloadFailureError — ожидаемый отказ тела (EXPECTED), отказ контракта
    запуска из конверта либо аргумент длиннее лимита argv
    (WrapErrorKind.ARGUMENT_TOO_LARGE).
LauncherError — исполнитель не отдал конверт; поднимает реализация порта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from functools import wraps
from typing import Any

from pydantic import BaseModel

from boba.toolkit.entry import (
    ArgumentTooLargeError,
    ReplyError,
    ToolAddress,
    ToolArgv,
    ToolLike,
)
from boba.toolkit.launcher import PayloadFailureError, ToolLauncher

__all__ = ["ToolProcessWrap", "WrapErrorKind"]


class WrapErrorKind(StrEnum):
    """Отказы обёртки до запуска процесса."""

    ARGUMENT_TOO_LARGE = "argument_too_large"


class ToolProcessWrap:
    """Подменяет тела инструмента переносом вызова в отдельный процесс."""

    @classmethod
    def guard_all(cls, tools: Sequence[ToolLike], launcher: ToolLauncher) -> None:
        for tool in tools:
            cls._guard(tool, launcher)

    @classmethod
    def _guard(cls, tool: ToolLike, launcher: ToolLauncher) -> None:
        address = ToolAddress.of(tool)
        schema = ToolArgv.schema_of(tool)

        call = cls._process_call(address, schema, launcher)

        # wraps сохраняет исходное тело в __wrapped__: каталог workflow читает
        # оттуда аннотацию результата (Produces)
        if tool.func is not None:
            cls._set_func(tool, wraps(tool.func)(call))

        if tool.coroutine is not None:

            @wraps(tool.coroutine)
            async def acall(**kwargs: object) -> object:
                return await asyncio.to_thread(lambda: call(**kwargs))

            cls._set_coroutine(tool, acall)

    @classmethod
    def _process_call(
        cls,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
    ) -> Callable[..., object]:
        def call(**kwargs: object) -> object:
            try:
                command = ToolArgv.render(address, schema, kwargs)
            except ArgumentTooLargeError as exc:
                raise PayloadFailureError(
                    str(WrapErrorKind.ARGUMENT_TOO_LARGE), str(exc)
                ) from exc

            outcome = launcher.run_tool(command)

            reply = outcome.reply
            if isinstance(reply, ReplyError):
                raise PayloadFailureError(reply.kind, reply.message)

            return reply.content, reply.artifact

        return call

    @staticmethod
    def _set_func(tool: ToolLike, body: Callable[..., Any]) -> None:
        """Подмена тела; у StructuredTool это обычные mutable-поля.

        Протокол ToolLike читающий (mutable-член инвариантен и отверг бы
        StructuredTool), поэтому запись идёт duck-typing'ом через Any.
        """
        owner: Any = tool
        owner.func = body

    @staticmethod
    def _set_coroutine(tool: ToolLike, body: Callable[..., Awaitable[Any]]) -> None:
        owner: Any = tool
        owner.coroutine = body
