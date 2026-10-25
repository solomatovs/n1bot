"""Обёртка запуска: тело инструмента плагина исполняется программой плагина.

Из объявления инструмента в манифесте (ToolDecl) собирается инструмент хоста,
чьё тело — перенос вызова: аргументы уезжают командой call программы плагина
через порт ToolLauncher накопительно (CollectedCall) — модели нужен итог, а
не кадры.

Ошибки:
PayloadFailureError — ошибка тела или контракта запуска из конверта либо
    срыв группы каналов; ошибка уже упакована в результат.
LauncherError — исполнитель не отдал конверт; поднимает реализация порта.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from functools import partial

from pydantic import create_model

from boba.toolkit.calls import CallViews, ToolCallBase
from boba.toolkit.chain import CallAmbient, NodeSlot
from boba.toolkit.entry import ToolArgv
from boba.toolkit.launcher import (
    CollectedCall,
    PayloadFailureError,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.manifest import ToolDecl
from boba.toolkit.protocol import CallGateMode, ReplyError, ToolCommand
from boba.toolkit.result import ToolResultBase
from boba.toolrun.hosted import HostedCall, HostedTool, PluginSection, ToolInputs

__all__ = ["ToolProcessWrap"]


class ToolProcessWrap:
    """Собирает инструмент хоста, чьё тело исполняет вызов программой плагина
    через ToolLauncher.

    Создаётся цепочкой обвязок (ToolChain) с обстановкой вызова процесса.
    hosted() берёт объявление инструмента из манифеста и секцию плагина и
    отдаёт HostedTool с телом-переносом: схема хоста пуста (её наполнят
    обвязки), аргументы модели идут входами инструмента; на вызове
    аргументы кодируются в ToolCommand (ToolArgv.render), вызов идёт
    накопительно (CollectedCall), конверт разворачивается в результат или
    PayloadFailureError. Перенос блокирующий и уходит в рабочий поток.

    Внутри конвейера (оркестратор поставил ручку в CallAmbient) вызов
    открывается потоково: каналы узла отдаются слоту дескрипторами, и данные
    текут между узлами мимо хоста; конверт разворачивается так же.
    """

    def __init__(self, ambient: CallAmbient) -> None:
        self._ambient = ambient
        self._views = CallViews()

    def hosted(
        self, decl: ToolDecl, launcher: ToolLauncher, section: PluginSection
    ) -> HostedTool:
        """Инструмент хоста с телом-переносом вызова исполнителю launcher."""
        schema = create_model(f"{decl.name}_call", __base__=ToolCallBase)

        return HostedTool(
            name=decl.name,
            description=decl.description,
            args_schema=schema,
            views=self._views.seen(decl.input_schema),
            body=partial(self._transfer, decl, launcher),
            hints=decl.hints,
            inputs=ToolInputs(
                passed=decl.input_schema,
                injected=decl.injected,
                connections=decl.connections,
                section=section,
            ),
        )

    async def _transfer(
        self, decl: ToolDecl, launcher: ToolLauncher, call: HostedCall
    ) -> ToolResultBase:
        return await asyncio.to_thread(self._process_call, decl, launcher, call.kwargs)

    def _process_call(
        self, decl: ToolDecl, launcher: ToolLauncher, kwargs: Mapping[str, object]
    ) -> ToolResultBase:
        slot = self._ambient.slot()
        if slot is not None:
            outcome = self._group_call(decl, launcher, slot, kwargs)
        else:
            outcome = self._single_call(decl, launcher, kwargs)

        reply = outcome.reply
        if isinstance(reply, ReplyError):
            raise PayloadFailureError(reply.failure)

        return reply.artifact

    def _single_call(
        self, decl: ToolDecl, launcher: ToolLauncher, kwargs: Mapping[str, object]
    ) -> ToolOutcome:
        """Вызов вне группы: накопительно."""
        command = ToolArgv.render(decl, kwargs, {}, {})

        return CollectedCall.of(launcher, command)

    def _group_call(
        self,
        decl: ToolDecl,
        launcher: ToolLauncher,
        slot: NodeSlot,
        kwargs: Mapping[str, object],
    ) -> ToolOutcome:
        """Вызов в группе: любой сбой до итога срывает группу, и вызов
        отвечает текстом её срыва; единственный вызов группы — своей
        ошибкой."""
        try:
            command = ToolArgv.render(
                decl, kwargs, slot.input_counts(), slot.output_counts()
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
