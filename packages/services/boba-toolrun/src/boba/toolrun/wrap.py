"""Обёртка запуска: тело инструмента исполняется отдельным процессом.

Из объявления фасада (PayloadTool) собирается инструмент хоста, чьё тело —
перенос вызова: по адресу модуля и схеме объявления вызов уезжает командой
модуля инструментов через порт ToolLauncher накопительно (CollectedCall) —
модели нужен итог, а не кадры.

Ошибки:
PayloadFailureError — ошибка тела или контракта запуска из конверта, срыв
    группы каналов либо аргумент длиннее лимита argv
    (WrapErrorKind.ARGUMENT_TOO_LARGE); ошибка уже упакована в результат.
LauncherError — исполнитель не отдал конверт; поднимает реализация порта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from enum import StrEnum
from functools import partial

from pydantic import BaseModel

from boba.toolkit.calls import CallViews
from boba.toolkit.chain import CallAmbient, NodeSlot
from boba.toolkit.entry import (
    ArgumentTooLargeError,
    ReplyError,
    ToolAddress,
    ToolArgv,
)
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import (
    CollectedCall,
    PayloadFailureError,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.ports import StreamSpec
from boba.toolkit.protocol import CallGateMode, ToolCommand
from boba.toolkit.result import ErrorResult, ToolResultBase
from boba.toolrun.hosted import HostedCall, HostedTool

__all__ = ["ToolProcessWrap", "WrapErrorKind"]


class WrapErrorKind(StrEnum):
    """Отказы обёртки до запуска процесса."""

    ARGUMENT_TOO_LARGE = "argument_too_large"


class ToolProcessWrap:
    """Собирает инструмент хоста, чьё тело исполняет вызов отдельным
    процессом через ToolLauncher.

    Создаётся цепочкой обвязок (ToolChain) с обстановкой вызова процесса.
    hosted() берёт у объявления фасада адрес модуля тела и полную схему и
    отдаёт HostedTool с телом-переносом: аргументы кодируются в ToolCommand
    (ToolArgv.render), вызов идёт накопительно (CollectedCall), конверт
    разворачивается в результат или PayloadFailureError. Перенос
    блокирующий и уходит в рабочий поток.

    Внутри конвейера (оркестратор поставил ручку в CallAmbient) вызов
    открывается потоково: каналы узла отдаются слоту дескрипторами, и данные
    текут между узлами мимо хоста; конверт разворачивается так же. spec_of()
    отдаёт потоковую декларацию объявления — позже injected-поля снимаются
    из видимой схемы, и портов в ней уже не найти.
    """

    def __init__(self, ambient: CallAmbient) -> None:
        self._ambient = ambient
        self._views = CallViews()

    def hosted(self, payload: PayloadTool, launcher: ToolLauncher) -> HostedTool:
        """Инструмент хоста с телом-переносом вызова исполнителю launcher."""
        address = ToolAddress.of(payload)
        schema = ToolArgv.schema_of(payload)

        return HostedTool(
            name=payload.name,
            description=payload.description,
            args_schema=schema,
            views=self._views.of(payload.args_schema),
            body=partial(self._transfer, address, schema, launcher),
            hints=payload.hints,
        )

    def spec_of(self, payload: PayloadTool) -> StreamSpec:
        """Потоковая декларация объявления: его порты каналов."""
        return StreamSpec.of_schema(ToolArgv.schema_of(payload))

    async def _transfer(
        self,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        call: HostedCall,
    ) -> ToolResultBase:
        return await asyncio.to_thread(
            self._process_call, address, schema, launcher, call.kwargs
        )

    def _process_call(
        self,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        kwargs: Mapping[str, object],
    ) -> ToolResultBase:
        slot = self._ambient.slot()
        if slot is not None:
            outcome = self._group_call(address, schema, launcher, slot, kwargs)
        else:
            outcome = self._single_call(address, schema, launcher, kwargs)

        reply = outcome.reply
        if isinstance(reply, ReplyError):
            raise PayloadFailureError(reply.failure)

        return reply.artifact

    def _single_call(
        self,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        kwargs: Mapping[str, object],
    ) -> ToolOutcome:
        """Вызов вне группы: накопительно."""
        command = self._render(address, schema, kwargs, {}, {})

        return CollectedCall.of(launcher, command)

    def _group_call(
        self,
        address: ToolAddress,
        schema: type[BaseModel],
        launcher: ToolLauncher,
        slot: NodeSlot,
        kwargs: Mapping[str, object],
    ) -> ToolOutcome:
        """Вызов в группе: любой сбой до итога срывает группу, и вызов
        отвечает текстом её срыва; единственный вызов группы — своей
        ошибкой."""
        try:
            command = self._render(
                address, schema, kwargs, slot.input_counts(), slot.output_counts()
            )
            held = slot.sized(command).model_copy(update={"gate": CallGateMode.HELD})
            return self._piped_call(launcher, held, slot)
        except BaseException as exc:
            verdict = slot.settle_error(exc)
            if verdict.stopped:
                raise

            if slot.solitary():
                raise

            raise PayloadFailureError(verdict.failure_of(slot.key)) from exc

    @staticmethod
    def _render(
        address: ToolAddress,
        schema: type[BaseModel],
        kwargs: Mapping[str, object],
        input_counts: Mapping[str, int],
        output_counts: Mapping[str, int],
    ) -> ToolCommand:
        try:
            return ToolArgv.render(address, schema, kwargs, input_counts, output_counts)
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

        Вызов с выходами открывается open_tap (выходные каналы хост не
        разбирает), входы забираются у вызова все разом; соединяет их
        раздача группы. Итог вызова уходит в группу, и ответ ждёт, пока
        решит вся группа.
        """
        outputs: tuple[int, ...] = ()
        if slot.has_outputs():
            tapped = launcher.open_tap(command)
            call = tapped.call
            outputs = tapped.frames_fds
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
