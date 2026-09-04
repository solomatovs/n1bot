"""Обёртка запуска: тело инструмента исполняется отдельным процессом.

Ставится первой, на нетронутое тело: захватывает адрес модуля, оригинальную
схему и само тело; вызов уезжает командой модуля инструментов через порт
ToolLauncher накопительно (CollectedCall) — модели нужен итог, а не кадры.

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

from boba.toolkit.chain import CallRelay, NodeSlot, PipelineSlot
from boba.toolkit.entry import (
    ArgumentTooLargeError,
    ReplyError,
    ToolAddress,
    ToolArgv,
    ToolLike,
)
from boba.toolkit.launcher import (
    CollectedCall,
    PayloadFailureError,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.ports import StreamSpec, ToolStreamSpecs
from boba.toolkit.protocol import ToolCommand

__all__ = ["ToolProcessWrap", "WrapErrorKind"]


class WrapErrorKind(StrEnum):
    """Отказы обёртки до запуска процесса."""

    ARGUMENT_TOO_LARGE = "argument_too_large"


class ToolProcessWrap:
    """Подменяет тело инструмента обёрткой, которая исполняет вызов отдельным
    процессом через ToolLauncher.

    LLM-агент зовёт tool-объект как обычную функцию; guard_all при сборке
    инструментов заменяет func/coroutine на перенос вызова: аргументы
    кодируются в ToolCommand (ToolArgv.render), вызов идёт накопительно
    (CollectedCall), конверт разворачивается в возврат или
    PayloadFailureError.

    Внутри конвейера (оркестратор поставил PipelineSlot) вызов открывается
    потоково: каналы узла отдаются слоту дескрипторами, и данные текут
    между узлами мимо хоста; конверт разворачивается так же. Попутно
    guard_all публикует потоковую декларацию инструмента в ToolStreamSpecs
    — позже injected-поля снимаются из видимой схемы, и портов в ней уже
    не найти.
    """

    @classmethod
    def guard_all(cls, tools: Sequence[ToolLike], launcher: ToolLauncher) -> None:
        for tool in tools:
            ToolStreamSpecs.register(
                tool.name, StreamSpec.of_schema(ToolArgv.schema_of(tool))
            )
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
                msg = f"tool {address.name!r}: {exc}"
                raise PayloadFailureError(
                    str(WrapErrorKind.ARGUMENT_TOO_LARGE), msg
                ) from exc

            slot = PipelineSlot.get()
            if slot is None:
                outcome = CollectedCall.of(launcher, command)
            else:
                outcome = cls._piped_call(launcher, command, slot)

            reply = outcome.reply
            if isinstance(reply, ReplyError):
                raise PayloadFailureError(reply.kind, reply.message)

            return reply.content, reply.artifact

        return call

    @staticmethod
    def _piped_call(
        launcher: ToolLauncher, command: ToolCommand, slot: NodeSlot
    ) -> ToolOutcome:
        """Вызов узла конвейера: каналы рёбер отдаются слоту дескрипторами.

        Выход узла с ребром вниз открывается open_tap (канал кадров хост не
        разбирает), вход узла с ребром вверх забирается у вызова — оба
        конца соединяет оркестратор splice'ом. Свободные каналы живут как в
        накопительном вызове: вход закрывается сразу, кадры дочитываются.
        """
        if slot.has_downstream:
            tapped = launcher.open_tap(command)
            call = tapped.call
            slot.give_source_fd(tapped.frames_fd)
        else:
            call = launcher.open(command)

        with call:
            slot.attach_abort(call.close)

            if slot.has_upstream:
                slot.give_input_fd(CallRelay.input_fd(call))
            else:
                call.done_sending()

            for _ in call.frames():
                continue

            return call.result()

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
