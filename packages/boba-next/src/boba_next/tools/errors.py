"""Иерархия ошибок выполнения tool'ов."""

from __future__ import annotations

from boba_next.tools.ids import ToolId, ToolSourceId

__all__ = [
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "ToolExecutionError",
    "ToolIdCollisionError",
    "ToolOutputTooLargeError",
]


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента."""

    def __init__(self, tool_id: ToolId, message: str) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.message = message


class InvalidToolArgumentError(ToolExecutionError):
    """Аргумент tool'а не прошёл per-param валидацию."""

    def __init__(self, tool_id: ToolId, param: str, reason: str) -> None:
        super().__init__(tool_id, f"параметр {param!r}: {reason}")
        self.param = param
        self.reason = reason


class ToolOutputTooLargeError(ToolExecutionError):
    """Tool отказался выдавать слишком большой результат."""

    def __init__(
        self,
        tool_id: ToolId,
        *,
        limit: int,
        unit: str,
        hint: str,
    ) -> None:
        super().__init__(
            tool_id,
            f"результат превышает лимит {limit} {unit}. {hint}",
        )
        self.limit = limit
        self.unit = unit
        self.hint = hint


class InvalidSchemaInvariantError(ToolExecutionError):
    """Cross-field инвариант схемы нарушен."""

    def __init__(self, tool_id: ToolId, reason: str) -> None:
        super().__init__(tool_id, f"нарушен инвариант схемы: {reason}")
        self.reason = reason


class ToolIdCollisionError(Exception):
    """Два источника пытаются зарегистрировать tool с одним ToolId."""

    def __init__(
        self,
        tool_id: ToolId,
        existing_source: ToolSourceId,
        new_source: ToolSourceId,
    ) -> None:
        super().__init__(
            f"tool id {tool_id.to_wire()!r} already registered "
            f"by source {existing_source.to_wire()!r}; "
            f"rejected new source {new_source.to_wire()!r}"
        )
        self.tool_id = tool_id
        self.existing_source = existing_source
        self.new_source = new_source
