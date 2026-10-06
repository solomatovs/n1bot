"""Обвязка инструментов хоста остановкой хода."""

from __future__ import annotations

from collections.abc import Sequence

from boba.cancellation import RunCancellation, current_cancellation
from boba.toolkit.result import ToolResultBase
from boba.toolrun.hosted import HostedCall, HostedTool
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["CancellableTools"]


class CancellableTools(CallHooks[RunCancellation]):
    """Обвязка отмены: не даёт инструменту стартовать после остановки запуска
    и вернуть результат. Ставится цепочкой обвязок (ToolChain)."""

    def __init__(self) -> None:
        self._bodies = ToolBody()

    def guard_all(self, tools: Sequence[HostedTool]) -> list[HostedTool]:
        return self._bodies.hook_all(tools, self)

    async def before(self, call: HostedCall) -> RunCancellation:
        cancellation = current_cancellation()
        cancellation.raise_if_cancelled()
        return cancellation

    async def after(
        self, ctx: RunCancellation, result: ToolResultBase
    ) -> ToolResultBase:
        ctx.raise_if_cancelled()
        return result
