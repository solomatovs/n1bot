"""Аварийное исключение инструмента -> ErrorResult: ход продолжается, LLM видит ошибку.

Инструменты, исполняемые в песочнице (doc/bash/chart), могут падать аварийно —
например, oom killer убивает payload. Без этой обёртки исключение доезжает до
callback'а и прерывает всю цепочку действий. Обёртка превращает его в ErrorResult:
ToolMessage уходит в историю, LLM видит текст ошибки и решает, что делать дальше.

Отказ (RefusalError) — не сбой: его текст написан для человека и LLM, поэтому
идёт в результат целиком и без технической цепочки причин, а kind результата —
kind отказа, а не имя класса исключения.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.toolkit.failure import FailureText, ToolRefusalError
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

    REFUSED: ClassVar[str] = "tool refused"
    """Отказ — не сбой: текст отказа идёт как есть, без цепочки причин."""

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
                message=cls._message(name, error),
                error_kind=cls._kind(error),
            )
        )

    @classmethod
    def _message(cls, name: str, error: Exception) -> str:
        """Текст для чата и истории: у отказа он уже написан для человека и LLM."""
        if isinstance(error, ToolRefusalError):
            return f"{cls.REFUSED} {name!r}: {error}"

        return f"{cls.PREFIX} {name!r}: {FailureText.of(error)}"

    @staticmethod
    def _kind(error: Exception) -> str:
        """Классификация: у отказа — его kind, у прочего — вид сбоя запуска."""
        if isinstance(error, ToolRefusalError):
            return error.kind

        return ErrorKind.of(error)
