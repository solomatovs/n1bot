"""ToolResult sealed-семейство + ToolResultVisitor (double-dispatch).

Result-типы и Visitor-протокол живут в одном модуле сознательно: это
закрытое sealed-семейство с фиксированной операцией (`accept`). Разнос
по двум модулям требовал бы forward-refs / TYPE_CHECKING — это маркер,
что разделение искусственно. Оставляем вместе.

Open-closed:
- Новый adapter (новый Visitor[T] под другой LLM-API) — без правок здесь.
- Новый Result-тип — расширение `ToolResultVisitor` методом `visit_*`,
  pyright требует реализацию во всех Visitor'ах (compile-time enforced).

`ToolResult` экспортируется как **type alias** на discriminated union
(`Annotated[TextResult | JsonResult | ErrorResult, Field(discriminator="kind")]`).
Это даёт строгую типизацию полей Pydantic-моделей и нативный JSON-Schema
oneOf с дискриминатором. Для runtime-проверок типа используй `isinstance(x,
(TextResult, JsonResult, ErrorResult))` либо обращайся к `ToolResultBase`
(публичный abstract base для наследования).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DefaultTextVisitor",
    "ErrorResult",
    "JsonResult",
    "TextResult",
    "ToolResult",
    "ToolResultBase",
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


class ToolResultBase(BaseModel, ABC):
    """Базовый Pydantic-класс для наследования конкретных ToolResult-вариантов.

    Не для использования как тип значения — используй `ToolResult` (alias
    на discriminated union). Этот класс публичен только для наследования
    при добавлении нового варианта в sealed-семейство.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @abstractmethod
    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        """Double-dispatch к соответствующему `visit_*` методу visitor'а."""
        ...


class TextResult(ToolResultBase):
    """Простой текст. Используется tools без структурированного payload'а."""

    kind: Literal["text"] = "text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_text(self)


class JsonResult(ToolResultBase):
    """JSON-сериализуемый payload. Visitor сам решает как форматировать."""

    kind: Literal["json"] = "json"
    payload: Any
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_json(self)


class ErrorResult(ToolResultBase):
    """Tool не выполнен: ошибка домена, отклонение guard'а, невалидные args.

    Отдельный sealed-вариант, чтобы провайдер мог рендерить ошибки иначе
    (например, для Anthropic — отдельный `is_error: true` блок).
    """

    kind: Literal["error"] = "error"
    message: str
    error_kind: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def accept(self, visitor: ToolResultVisitor[T]) -> T:
        return visitor.visit_error(self)


ToolResult: TypeAlias = Annotated[
    TextResult | JsonResult | ErrorResult, Field(discriminator="kind"),
]
"""Тип значения tool-результата: discriminated union по полю `kind`.

Используй для типизации полей моделей, возвращаемых значений функций и
параметров. Pydantic генерирует `oneOf` JSON Schema из коробки.
"""


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
