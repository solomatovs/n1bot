"""ToolResult sealed-семейство + ToolResultVisitor (double-dispatch).

Result-типы и Visitor-протокол живут в одном модуле сознательно: это
закрытое sealed-семейство с фиксированной операцией (`accept`). Разнос
по двум модулям требовал бы forward-refs / TYPE_CHECKING — это маркер,
что разделение искусственно. Оставляем вместе.

Open-closed:
- Новый adapter (новый Visitor[T] под другой LLM-API) — без правок здесь.
- Новый Result-тип — расширение `ToolResultVisitor` методом `visit_*`,
  pyright требует реализацию во всех Visitor'ах (compile-time enforced).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

__all__ = [
    "DefaultTextVisitor",
    "ErrorResult",
    "JsonResult",
    "TextResult",
    "ToolResult",
    "ToolResultVisitor",
]

T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")


class ToolResultVisitor(Protocol[T_co]):
    """Visitor над семейством ToolResult-подтипов.

    Adapter под конкретный LLM-API (chat / responses / anthropic) реализует
    Visitor[T] с своим target-типом T (`str`, `list[Block]`, `dict`, ...).
    """

    def visit_text(self, result: TextResult) -> T_co: ...
    def visit_json(self, result: JsonResult) -> T_co: ...
    def visit_error(self, result: ErrorResult) -> T_co: ...


class ToolResult(ABC):
    """База для всех типизированных результатов tool'а."""

    @abstractmethod
    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        """Double-dispatch к соответствующему `visit_*` методу visitor'а."""
        ...


@dataclass(frozen=True)
class TextResult(ToolResult):
    """Простой текст. Используется tools без структурированного payload'а."""

    text: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_text(self)


@dataclass(frozen=True)
class JsonResult(ToolResult):
    """JSON-сериализуемый payload. Visitor сам решает как форматировать."""

    payload: Any
    metadata: Mapping[str, str] = field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_json(self)


@dataclass(frozen=True)
class ErrorResult(ToolResult):
    """Tool не выполнен: ошибка домена, отклонение guard'а, невалидные args.

    Отдельный sealed-вариант, чтобы провайдер мог рендерить ошибки иначе
    (например, для Anthropic — отдельный `is_error: true` блок).
    """

    message: str
    error_kind: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_error(self)


class DefaultTextVisitor(ToolResultVisitor[str]):
    """Провайдер-агностичный текстовый рендер для UI/логов/persistence.

    Не для wire-формата — каждый провайдер реализует свой Visitor.
    """

    def visit_text(self, result: TextResult) -> str:
        return result.text

    def visit_json(self, result: JsonResult) -> str:
        return json.dumps(result.payload, ensure_ascii=False)

    def visit_error(self, result: ErrorResult) -> str:
        return result.message
