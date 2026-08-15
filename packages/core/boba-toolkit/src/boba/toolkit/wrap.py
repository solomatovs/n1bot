"""Обёртка запуска: тело инструмента исполняется отдельным процессом.

Ставится первой, на нетронутое тело: захватывает адрес модуля, оригинальную
схему и само тело. С launcher'ом вызов уезжает командой модуля инструментов
через порт ToolLauncher; без него (секции песочницы нет — dev-режим) тело
зовётся прямо в процессе, а ожидаемые исключения переводятся в тот же
PayloadFailureError — kind отказа не зависит от места исполнения.

Ошибки:
PayloadFailureError — ожидаемый отказ тела (EXPECTED), отказ контракта
    запуска из конверта либо аргумент длиннее лимита argv
    (WrapErrorKind.ARGUMENT_TOO_LARGE).
LauncherError — исполнитель не отдал конверт; поднимает реализация порта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from boba.toolkit.entry import (
    ArgumentTooLargeError,
    ExpectedErrors,
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
    def guard_all(
        cls,
        tools: Sequence[ToolLike],
        launcher: ToolLauncher | None,
    ) -> None:
        for tool in tools:
            cls._guard(tool, launcher)

    @classmethod
    def _guard(cls, tool: ToolLike, launcher: ToolLauncher | None) -> None:
        address = ToolAddress.of(tool)
        schema = ToolArgv.schema_of(tool)

        original_func = tool.func
        original_coroutine = tool.coroutine

        if launcher is None:
            cls._guard_local(tool, original_func, original_coroutine)
            return

        call = cls._process_call(address, schema, launcher)

        if original_func is not None:
            cls._set_func(tool, call)

        if original_coroutine is not None:

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

    @classmethod
    def _guard_local(
        cls,
        tool: ToolLike,
        original_func: Callable[..., Any] | None,
        original_coroutine: Callable[..., Awaitable[Any]] | None,
    ) -> None:
        """Локальный режим: тело в процессе, EXPECTED — тем же kind'ом."""
        if original_func is not None:
            expected = ExpectedErrors.of_body(original_func)

            def call(
                *args: object,
                _body: Callable[..., Any] = original_func,
                _expected: Mapping[type[Exception], str] = expected,
                **kwargs: object,
            ) -> object:
                try:
                    return _body(*args, **kwargs)
                except Exception as exc:
                    cls._raise_expected(exc, _expected)
                    raise

            cls._set_func(tool, call)

        if original_coroutine is not None:
            aexpected = ExpectedErrors.of_body(original_coroutine)

            async def acall(
                *args: object,
                _body: Callable[..., Awaitable[Any]] = original_coroutine,
                _expected: Mapping[type[Exception], str] = aexpected,
                **kwargs: object,
            ) -> object:
                try:
                    return await _body(*args, **kwargs)
                except Exception as exc:
                    cls._raise_expected(exc, _expected)
                    raise

            cls._set_coroutine(tool, acall)

    @staticmethod
    def _raise_expected(
        exc: Exception, expected: Mapping[type[Exception], str]
    ) -> None:
        """Ожидаемое исключение — единый конверт отказа; прочие проходят."""
        if isinstance(exc, PayloadFailureError):
            return

        kind = ExpectedErrors.kind_of(exc, expected)
        if kind is None:
            return

        raise PayloadFailureError(kind, str(exc)) from exc

    @staticmethod
    def _set_func(tool: ToolLike, body: Callable[..., Any]) -> None:
        """Подмена тела; у StructuredTool это обычные mutable-поля.

        Протокол ToolLike читающий (mutable-член инвариантен и отверг бы
        StructuredTool), поэтому запись идёт duck-typing'ом через Any.
        """
        owner: Any = tool
        owner.func = body

    @staticmethod
    def _set_coroutine(
        tool: ToolLike, body: Callable[..., Awaitable[Any]]
    ) -> None:
        owner: Any = tool
        owner.coroutine = body
