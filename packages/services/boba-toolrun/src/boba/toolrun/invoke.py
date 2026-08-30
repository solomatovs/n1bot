"""Исполнитель вызова инструмента вне хода чата: REST, workflow, планировщик.

Инструменты — уже собранные реестром с полной цепочкой хуков и отобранные
под субъекта (ToolRegistry.for_headless). Исполнитель собирает ToolCall со
служебными полями (id, intent), зовёт инструмент и разбирает ответ в
InvokeReply. Контекст и запуск открывает вызывающий: RunRegistry.open(context).

Ошибки:
ToolUnavailableError — инструмента нет среди видимых субъекту вне чата.
ToolContractError — инструмент вернул не ToolMessage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.failure import (
    InvokeErrorKind,
    ToolContractError,
    ToolUnavailableError,
)
from boba.toolkit.result import ErrorResult, ToolArtifact, ToolResult

__all__ = [
    "InvokeReply",
    "ToolInvoker",
]


class InvokeReply(BaseModel):
    """Ответ инструмента, разобранный один раз: сообщение и модель результата."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    message: ToolMessage
    result: ToolResult

    @classmethod
    def of(cls, message: ToolMessage) -> InvokeReply:
        result = ToolArtifact.revive(message.artifact)
        if result is None:
            result = ErrorResult(
                message=str(message.content), error_kind=InvokeErrorKind.NO_RESULT
            )

        return cls(message=message, result=result)

    @property
    def content(self) -> str:
        return str(self.message.content)

    @property
    def ok(self) -> bool:
        if self.message.status == "error":
            return False

        return self.result.ok

    @property
    def error_text(self) -> str:
        """Текст отказа для журнала и рёбер; пустой — вызов удался."""
        if self.message.status == "error":
            return self.content

        if not self.result.ok:
            return self.result.llm_text()

        return ""


class ToolInvoker:
    """Вызовы инструментов, видимых субъекту вне чата."""

    def __init__(self, tools: Mapping[str, BaseTool]) -> None:
        self._tools = dict(tools)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def tool(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolUnavailableError(f"tool {name!r} is not available")

        return tool

    @staticmethod
    def call(
        name: str, args: Mapping[str, Any], intent: str, prefix: CallIdPrefix
    ) -> ToolCall:
        call_args: dict[str, Any] = dict(args)
        call_args[ToolIntent.NAME] = intent

        return ToolCall(name=name, args=call_args, id=prefix.new_id(), type="tool_call")

    async def invoke(self, call: ToolCall) -> InvokeReply:
        """Вызов вне дерева колбэков вызывающего: из хода чата задачи workflow
        в ленту не попадают, их итог несёт отчёт самого запуска."""
        tool = self.tool(call["name"])

        detached = var_child_runnable_config.set(None)
        try:
            message = await tool.ainvoke(call)
        finally:
            var_child_runnable_config.reset(detached)

        if not isinstance(message, ToolMessage):
            got = type(message).__name__
            raise ToolContractError(f"tool {call['name']!r} returned {got}")

        return InvokeReply.of(message)
