"""Аварийное исключение инструмента -> ErrorResult: ход продолжается, LLM видит ошибку.

Инструменты, исполняемые в песочнице (doc/bash/chart), могут падать аварийно —
например, oom killer убивает payload. Без этой обёртки исключение доезжает до
callback'а и прерывает всю цепочку действий. Обёртка превращает его в ErrorResult:
ToolMessage уходит в историю, LLM видит текст ошибки и решает, что делать дальше.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.toolkit.failure import FailureText
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

    class _Hooks(CallHooks[str]):
        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> str:
            return name

        def on_error(self, ctx: str, error: Exception) -> object:
            return ToolErrorGuard._failure(ctx, error)

    @classmethod
    def guard_all(cls, tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.hook_all(tools, cls._Hooks())

    @classmethod
    def _failure(cls, name: str, error: Exception) -> tuple[str, ToolResult]:
        return pack_result(
            ErrorResult(
                message=f"{cls.PREFIX} {name!r}: {FailureText.of(error)}",
                error_kind=ErrorKind.of(error),
            )
        )
