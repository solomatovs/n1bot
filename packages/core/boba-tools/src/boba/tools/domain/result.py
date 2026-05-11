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

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

__all__ = ["JsonResult", "TextResult", "ToolResult", "ToolResultVisitor"]

T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")


class ToolResultVisitor(Protocol[T_co]):
    """Visitor над семейством ToolResult-подтипов.

    Adapter под конкретный LLM-API (chat / responses / anthropic) реализует
    Visitor[T] с своим target-типом T (`str`, `list[Block]`, `dict`, ...).
    """

    def visit_text(self, result: TextResult) -> T_co: ...
    def visit_json(self, result: JsonResult) -> T_co: ...


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
