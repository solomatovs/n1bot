"""Аварийное исключение инструмента -> ErrorResult: ход продолжается, LLM видит ошибку.

Инструменты, исполняемые в песочнице (doc/bash/chart), могут падать аварийно —
например, oom killer убивает payload. Без этой обёртки исключение доезжает до
callback'а и прерывает всю цепочку действий. Обёртка превращает его в ErrorResult:
ToolMessage уходит в историю, LLM видит текст ошибки и решает, что делать дальше.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import AsyncCall, SyncCall, ToolBody
from boba.toolkit.launcher import ErrorKind
from boba.toolkit.result import ErrorResult, ToolResult, pack_result

__all__ = ["ToolErrorGuard"]


class ToolErrorGuard:
    """Ловит исключение выполнения инструмента и возвращает его как ErrorResult.

    Не перехватывает BaseException: ToolStopped (отмена хода) и
    asyncio.CancelledError должны прерывать инструмент, а не становиться
    «ошибкой» для LLM.
    """

    PREFIX: ClassVar[str] = "tool failed"

    @staticmethod
    def guard_all(tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.wrap_all(
            tools, ToolErrorGuard._wrap, ToolErrorGuard._wrap_async
        )

    @staticmethod
    def _wrap(call: SyncCall, name: str) -> SyncCall:
        @wraps(call)
        def wrapped(*args: object, **kwargs: object) -> object:
            try:
                return call(*args, **kwargs)
            except Exception as e:
                return ToolErrorGuard._failure(name, e)

        return wrapped

    @staticmethod
    def _wrap_async(call: AsyncCall, name: str) -> AsyncCall:
        @wraps(call)
        async def wrapped(*args: object, **kwargs: object) -> object:
            try:
                return await call(*args, **kwargs)
            except Exception as e:
                return ToolErrorGuard._failure(name, e)

        return wrapped

    @classmethod
    def _failure(cls, name: str, error: Exception) -> tuple[str, ToolResult]:
        return pack_result(
            ErrorResult(
                message=f"{cls.PREFIX} {name!r}: {error}",
                error_kind=ErrorKind.of(error),
            )
        )
