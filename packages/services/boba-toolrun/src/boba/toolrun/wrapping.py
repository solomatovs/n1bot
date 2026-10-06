"""Постановка обвязок на тело инструмента хоста.

Обвязки (логи, права, отмена, ошибки, источники значений) отличаются только
крючками вокруг вызова, а способ постановки один: тело инструмента хоста
одно и асинхронное, обвязка оборачивает его своими крючками CallHooks.
Крючки получают вызов объектом HostedCall — имя инструмента, идентификатор
вызова и аргументы тела.

Ошибки: своих не выпускает; исключения крючков идут наверх как есть.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from boba.toolkit.result import ToolResultBase
from boba.toolrun.hosted import AsyncCall, HostedCall, HostedTool

__all__ = ["CallHooks", "ToolBody"]


CallCtx = TypeVar("CallCtx")
"""Контекст вызова, который обвязка заводит в before и читает в остальном."""


class CallHooks(Generic[CallCtx]):
    """Крючки одной обвязки вокруг вызова тела; база — сквозной проход.

    Базовый класс обвязок цепочки (журнал, права, отмена, упаковка ошибок)
    и источников значений (AsyncInjected); ставит их на тело ToolBody.
    before возвращает контекст вызова, он же приходит в остальные крючки —
    его тип обвязка объявляет параметром, поэтому приведения типа внутри
    крючков не нужны. on_error либо поднимает ошибку дальше, либо возвращает
    замену результата; cleanup выполняется всегда, после after или on_error.
    """

    async def before(self, call: HostedCall) -> CallCtx:
        raise NotImplementedError

    async def after(self, ctx: CallCtx, result: ToolResultBase) -> ToolResultBase:
        return result

    async def on_error(self, ctx: CallCtx, error: Exception) -> ToolResultBase:
        raise error

    async def cleanup(self, ctx: CallCtx) -> None:
        return


class ToolBody:
    """Тела инструментов хоста под обвязками.

    Создаётся каждой обвязкой и источником значений, которым нужно лечь на
    тело инструмента (журнал, права, отмена, ошибки, параметры-соединения).
    """

    def hook_all(
        self, tools: Sequence[HostedTool], hooks: CallHooks[Any]
    ) -> list[HostedTool]:
        """Ставит одну обвязку крючками на тело каждого инструмента; порядок
        списка сохраняется."""
        hooked: list[HostedTool] = []
        for tool in tools:
            tool.body = self._guarded(tool.body, hooks)
            hooked.append(tool)

        return hooked

    @staticmethod
    def _guarded(body: AsyncCall, hooks: CallHooks[Any]) -> AsyncCall:
        async def guarded(call: HostedCall) -> ToolResultBase:
            ctx = await hooks.before(call)
            try:
                result = await body(call)
            except Exception as e:
                return await hooks.on_error(ctx, e)
            else:
                return await hooks.after(ctx, result)
            finally:
                await hooks.cleanup(ctx)

        return guarded
