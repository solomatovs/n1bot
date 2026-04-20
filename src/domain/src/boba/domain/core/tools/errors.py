"""Иерархия ошибок выполнения tool'ов.

Все tool-ошибки — потомки :class:`ToolExecutionError`. Agent-слой
(middleware) ловит родительский тип, достаёт ``error_kind`` из
``type(e).__name__`` и превращает в ``ToolFeedbackError`` для LLM —
разные подклассы дают разные ``error_kind``, LLM видит конкретику.
"""

from __future__ import annotations

from boba.domain.core.tools.schema import ToolId, ToolSourceId


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента.

    Бросается из ``ToolsService.execute`` / ``Tool.execute`` вместо
    возврата флагового результата. Обработка — на стороне caller-а:
    agent ловит, обогащает ``tool_call_id``-ом (который сервис не знает)
    и превращает в feedback-сообщение для LLM.
    """

    def __init__(self, tool_id: ToolId, message: str) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.message = message


class InvalidToolArgumentError(ToolExecutionError):
    """Аргумент tool'а не прошёл per-param валидацию.

    Бросается оркестратором ``SchemaArgsValidator`` при отсутствии
    обязательного параметра, нарушении типа, выходе за enum, незнакомом
    ключе и т.п. Хранит имя проблемного параметра в ``param``.
    """

    def __init__(self, tool_id: ToolId, param: str, reason: str) -> None:
        super().__init__(tool_id, f"параметр {param!r}: {reason}")
        self.param = param
        self.reason = reason


class InvalidSchemaInvariantError(ToolExecutionError):
    """Cross-field инвариант схемы нарушен.

    Отличается от :class:`InvalidToolArgumentError` тем, что проблема не
    в одном параметре, а в сочетании нескольких (взаимоисключение,
    совместность, порядок). Для LLM это даёт отдельный ``error_kind``,
    чтобы фидбек отличался по формулировке.
    """

    def __init__(self, tool_id: ToolId, reason: str) -> None:
        super().__init__(tool_id, f"нарушен инвариант схемы: {reason}")
        self.reason = reason


class ToolIdCollisionError(Exception):
    """Два источника пытаются зарегистрировать tool с одним :class:`ToolId`."""

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
