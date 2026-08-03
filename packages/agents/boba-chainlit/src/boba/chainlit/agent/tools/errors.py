"""Аварийное исключение инструмента -> ErrorResult: ход продолжается, LLM видит ошибку.

Инструменты, исполняемые в песочнице (doc/bash/chart), могут падать аварийно —
например, oom killer убивает payload. Без этой обёртки исключение доезжает до
callback'а и прерывает всю цепочку действий. Обёртка превращает его в ErrorResult:
ToolMessage уходит в историю, LLM видит текст ошибки и решает, что делать дальше.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ClassVar, cast

from langchain_core.tools import BaseTool

from boba.toolkit.pack import pack_result
from boba.toolkit.result import ErrorResult

__all__ = ["ToolErrorGuard"]


class ToolErrorGuard:
    """Ловит исключение выполнения инструмента и возвращает его как ErrorResult.

    Не перехватывает BaseException: ToolStopped (отмена хода) и
    asyncio.CancelledError должны прерывать инструмент, а не становиться
    «ошибкой» для LLM.
    """

    PREFIX: ClassVar[str] = "tool failed"

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = ToolErrorGuard._wrap(func, tool.name)
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )
            if callable(coroutine):
                tool.coroutine = ToolErrorGuard._wrap_async(coroutine, tool.name)
        return tools

    @staticmethod
    def _wrap(func: Callable[..., object], name: str) -> Callable[..., object]:
        @wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                return ToolErrorGuard._failure(name, e)

        return wrapped

    @staticmethod
    def _wrap_async(
        coroutine: Callable[..., Awaitable[object]],
        name: str,
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def wrapped(*args: object, **kwargs: object) -> object:
            try:
                return await coroutine(*args, **kwargs)
            except Exception as e:
                return ToolErrorGuard._failure(name, e)

        return wrapped

    @classmethod
    def _failure(cls, name: str, error: Exception) -> tuple[str, ErrorResult]:
        return pack_result(
            ErrorResult(
                message=f"{cls.PREFIX} {name!r}: {error}",
                error_kind=type(error).__name__,
            )
        )
