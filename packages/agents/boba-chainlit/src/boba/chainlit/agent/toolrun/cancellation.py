"""Обвязка langchain-инструментов остановкой хода."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from boba.cancellation import TurnCancellation, current_cancellation
from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["CancellableTools"]


class CancellableTools:
    """Не даёт инструменту стартовать после остановки и вернуть результат."""

    class _Hooks(CallHooks[TurnCancellation]):
        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> TurnCancellation:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            return cancellation

        def after(self, ctx: TurnCancellation, result: object) -> object:
            ctx.raise_if_cancelled()
            return result

    @classmethod
    def guard_all(cls, tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.hook_all(tools, cls._Hooks())
