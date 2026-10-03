"""Обвязка langchain-инструментов остановкой хода."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from boba.cancellation import RunCancellation, current_cancellation
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["CancellableTools"]


class CancellableTools(CallHooks[RunCancellation]):
    """Обвязка отмены: не даёт инструменту стартовать после остановки запуска
    и вернуть результат. Ставится цепочкой обвязок (ToolChain)."""

    def guard_all(self, tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.hook_all(tools, self)

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> RunCancellation:
        cancellation = current_cancellation()
        cancellation.raise_if_cancelled()
        return cancellation

    def after(self, ctx: RunCancellation, result: object) -> object:
        ctx.raise_if_cancelled()
        return result
