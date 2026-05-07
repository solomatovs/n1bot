"""Иерархия ошибок выполнения tool'ов.

Все ошибки выполнения держат `tool_id: ToolId` — qualified wire-id
(`<source>/<name>`). Tool сам владеет своим `ToolId` через ctor.
"""

from __future__ import annotations

from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId

__all__ = [
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "ToolExecutionError",
    "ToolIdCollisionError",
    "ToolOutputTooLargeError",
    "ToolSourceCollisionError",
]


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента (включая dispatch-фейлы вроде not-found)."""

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
    """Внутри одного source — два tool'а с одинаковым `ToolName`."""

    def __init__(self, source_id: ToolSourceId, name: ToolName) -> None:
        super().__init__(
            f"source {source_id.to_wire()!r} declares tool "
            f"{name.to_wire()!r} more than once"
        )
        self.source_id = source_id
        self.name = name


class ToolSourceCollisionError(Exception):
    """Два source'а с одинаковым `ToolSourceId` в одном `ToolsService`."""

    def __init__(self, source_id: ToolSourceId) -> None:
        super().__init__(
            f"duplicate tool source {source_id.to_wire()!r}",
        )
        self.source_id = source_id
