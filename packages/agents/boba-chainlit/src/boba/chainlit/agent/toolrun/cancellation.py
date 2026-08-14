"""Обвязка langchain-инструментов остановкой хода."""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps

from langchain_core.tools import BaseTool

from boba.cancellation import current_cancellation
from boba.chainlit.agent.toolrun.wrapping import AsyncCall, SyncCall, ToolBody

__all__ = ["CancellableTools"]


class CancellableTools:
    """Не даёт инструменту стартовать после остановки и вернуть результат."""

    @staticmethod
    def guard_all(tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.wrap_all(
            tools, CancellableTools._guard, CancellableTools._guard_async
        )

    @staticmethod
    def _guard(call: SyncCall, _name: str) -> SyncCall:
        @wraps(call)
        def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = call(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded

    @staticmethod
    def _guard_async(call: AsyncCall, _name: str) -> AsyncCall:
        @wraps(call)
        async def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = await call(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded
