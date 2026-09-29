"""Любое исключение инструмента -> результат-ошибка: ход продолжается, LLM видит ошибку.

Инструменты падают по разным причинам — отказ сервера, дефект кода, oom
killer в песочнице. Без этой обёртки исключение доезжает до callback'а и
прерывает всю цепочку действий. Обёртка упаковывает его FailurePacker'ом в
наследника FailureResult: ToolMessage уходит в историю, чат рисует тот же
результат, LLM видит текст ошибки и решает, что делать дальше.

Ошибки: не выпускает; ToolStopped и asyncio.CancelledError проходят насквозь.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import FailureResult
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["ToolErrorGuard"]


class ToolErrorGuard(CallHooks[str]):
    """Ловит исключение выполнения инструмента и возвращает его результатом-ошибкой.

    Не перехватывает BaseException: ToolStopped (отмена хода) и
    asyncio.CancelledError должны прерывать инструмент, а не становиться
    «ошибкой» для LLM. Ставится последней обвязкой при сборке инструментов
    (ToolLoader); вид ошибки целиком решает упаковщик.
    """

    def __init__(self) -> None:
        self._failures = FailurePacker()

    def guard_all(self, tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.hook_all(tools, self)

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> str:
        return name

    def on_error(self, ctx: str, error: Exception) -> tuple[str, FailureResult]:
        return self._failures.pack(error).packed()
