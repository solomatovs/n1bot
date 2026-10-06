"""Любое исключение инструмента -> результат-ошибка: ход продолжается, LLM видит ошибку.

Инструменты падают по разным причинам — отказ сервера, дефект кода, oom
killer в песочнице. Без этой обёртки исключение доезжает до callback'а и
прерывает всю цепочку действий. Обёртка упаковывает его FailurePacker'ом в
наследника FailureResult: итог вызова уходит в историю, чат рисует тот же
результат, LLM видит текст ошибки и решает, что делать дальше.

Ошибки: не выпускает; ToolStopped и asyncio.CancelledError проходят насквозь.
"""

from __future__ import annotations

from collections.abc import Sequence

from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import FailureResult
from boba.toolrun.hosted import HostedCall, HostedTool
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
        self._bodies = ToolBody()

    def guard_all(self, tools: Sequence[HostedTool]) -> list[HostedTool]:
        return self._bodies.hook_all(tools, self)

    async def before(self, call: HostedCall) -> str:
        return call.tool

    async def on_error(self, ctx: str, error: Exception) -> FailureResult:
        return self._failures.pack(error)
