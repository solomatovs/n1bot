"""Иерархия ошибок выполнения tool'ов.

Все ошибки выполнения держат tool_id: ToolId — qualified wire-id
(<source>/<name>). Tool сам владеет своим ToolId через ctor.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from boba.tools.domain.ids import ToolId

__all__ = [
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "ToolExecutionError",
    "ToolOutputTooLargeError",
]


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента (включая dispatch-фейлы вроде not-found)."""

    def __init__(self, tool_id: ToolId, message: str) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.message = message


class InvalidToolArgumentError(ToolExecutionError):
    """Аргумент tool'а не прошёл per-param валидацию.

    Сообщение для LLM содержит:
    - имя параметра и причину;
    - JSON-Schema fragment (что ожидалось), если доступно;
    - preview полученного значения, чтобы модель видела что отправила.
    """

    _RECEIVED_PREVIEW_LIMIT: ClassVar[int] = 200
    _UNSET: ClassVar[Any] = object()

    def __init__(
        self,
        tool_id: ToolId,
        param: str,
        reason: str,
        *,
        expected: dict[str, Any] | None = None,
        received: Any = _UNSET,
    ) -> None:
        message = self._format(param, reason, expected, received)
        super().__init__(tool_id, message)
        self.param = param
        self.reason = reason
        self.expected = expected
        self.received = received if received is not self._UNSET else None

    @classmethod
    def _format(
        cls,
        param: str,
        reason: str,
        expected: dict[str, Any] | None,
        received: Any,
    ) -> str:
        parts = [f"параметр {param!r}: {reason}"]
        if expected is not None:
            parts.append(
                f"ожидалось (JSON-Schema): "
                f"{json.dumps(expected, ensure_ascii=False)}"
            )
        if received is not cls._UNSET:
            preview = json.dumps(received, ensure_ascii=False, default=str)
            if len(preview) > cls._RECEIVED_PREVIEW_LIMIT:
                preview = preview[: cls._RECEIVED_PREVIEW_LIMIT] + "…"
            parts.append(f"получено: {preview}")
        return "; ".join(parts)


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
