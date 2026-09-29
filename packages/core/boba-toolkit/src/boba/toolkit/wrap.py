"""Обёртка запуска: тело инструмента исполняется отдельным процессом.

Ставится первой, на нетронутое тело: захватывает адрес модуля, оригинальную
схему и само тело; вызов уезжает командой модуля инструментов через порт
ToolLauncher накопительно (CollectedCall) — модели нужен итог, а не кадры.

Ошибки:
PayloadFailureError — ошибка тела или контракта запуска из конверта, срыв
    группы каналов либо аргумент длиннее лимита argv
    (WrapErrorKind.ARGUMENT_TOO_LARGE); ошибка уже упакована в результат.
LauncherError — исполнитель не отдал конверт; поднимает реализация порта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from functools import wraps
from typing import Any

from pydantic import BaseModel

from boba.toolkit.chain import NodeSlot, PipelineSlot
from boba.toolkit.entry import (
    ArgumentTooLargeError,
    ReplyError,
    ToolAddress,
    ToolArgv,
    ToolLike,
)
from boba.toolkit.launcher import (
    CollectedCall,
    FrameTap,
    ObservedCall,
    PayloadFailureError,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.ports import StreamSpec, ToolStreamSpecs
from boba.toolkit.protocol import CallGateMode, ToolCommand
from boba.toolkit.result import ErrorResult

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
    между узлами мимо хоста; конверт разворачивается так же. Если
    вызывающий поставил приёмник кадров (FrameTap), кадры тела отдаются ему
    по одному (ObservedCall) — так хост читает результат, который тело шлёт
    кадрами, а не конвертом. Попутно
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
        # оттуда аннотацию результата
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
            slot = PipelineSlot.get()
            if slot is not None:
                outcome = cls._group_call(address, schema, launcher, slot, kwargs)
            else:
                outcome = cls._single_call(address, schema, launcher, kwargs)

            reply = outcome.reply
            if isinstance(reply, ReplyError):
                raise PayloadFailureError(reply.failure)

            return reply.content, reply.artifact

        return call

    @classmethod
    def _single_call(
        cls,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        kwargs: Mapping[str, object],
    ) -> ToolOutcome:
        """Вызов вне группы: накопительно либо с приёмником кадров."""
        command = cls._render(address, schema, kwargs, {})

        if sink := FrameTap.get():
            return ObservedCall.of(launcher, command, sink)

        return CollectedCall.of(launcher, command)

    @classmethod
    def _group_call(
        cls,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        slot: NodeSlot,
        kwargs: Mapping[str, object],
    ) -> ToolOutcome:
        """Вызов в группе: любой сбой до итога срывает группу, и вызов
        отвечает текстом её срыва."""
        try:
            command = cls._render(address, schema, kwargs, slot.input_counts())
            held = command.model_copy(update={"gate": CallGateMode.HELD})
            return cls._piped_call(launcher, held, slot)
        except BaseException as exc:
            verdict = slot.settle_error(exc)
            if verdict.stopped:
                raise

            raise PayloadFailureError(verdict.failure_of(slot.key)) from exc

    @staticmethod
    def _render(
        address: ToolAddress,
        schema: type[BaseModel],
        kwargs: Mapping[str, object],
        input_counts: Mapping[str, int],
    ) -> ToolCommand:
        try:
            return ToolArgv.render(address, schema, kwargs, input_counts)
        except ArgumentTooLargeError as exc:
            failure = ErrorResult(
                message=f"tool {address.name!r}: {exc}",
                error_kind=WrapErrorKind.ARGUMENT_TOO_LARGE,
            )
            raise PayloadFailureError(failure) from exc

    @staticmethod
    def _piped_call(
        launcher: ToolLauncher, command: ToolCommand, slot: NodeSlot
    ) -> ToolOutcome:
        """Вызов группы: каналы отдаются ей дескрипторами, итог — по её решению.

        Вызов с выходом открывается open_tap (канал кадров хост не
        разбирает), входы забираются у вызова все разом; соединяет их
        раздача группы. Итог вызова уходит в группу, и ответ ждёт, пока
        решит вся группа.
        """
        outputs: tuple[int, ...] = ()
        if slot.has_downstream:
            tapped = launcher.open_tap(command)
            call = tapped.call
            outputs = (tapped.frames_fd,)
        else:
            call = launcher.open(command)

        with call:
            inputs = tuple(ToolProcessWrap._input_fds(call))
            call.done_sending()
            slot.attach(call, outputs, inputs)

            for _ in call.frames():
                continue

            outcome = call.result()

        return slot.settle(outcome)

    @staticmethod
    def _input_fds(call: ToolCall) -> Iterator[int]:
        for entry in call.inputs():
            yield entry.take_fd()

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
