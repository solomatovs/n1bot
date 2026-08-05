"""Обвязка langchain-инструментов остановкой хода."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import cast

from langchain_core.tools import BaseTool

from boba.cancellation import current_cancellation

__all__ = ["CancellableTools"]


class CancellableTools:
    """Не даёт инструменту стартовать после остановки и вернуть результат."""

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = CancellableTools._guard(func)
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )
            if callable(coroutine):
                tool.coroutine = CancellableTools._guard_async(coroutine)
        return tools

    @staticmethod
    def _guard(func: Callable[..., object]) -> Callable[..., object]:
        @wraps(func)
        def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = func(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded

    @staticmethod
    def _guard_async(
        coroutine: Callable[..., Awaitable[object]],
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def guarded(*args: object, **kwargs: object) -> object:
            cancellation = current_cancellation()
            cancellation.raise_if_cancelled()
            result = await coroutine(*args, **kwargs)
            cancellation.raise_if_cancelled()
            return result

        return guarded
