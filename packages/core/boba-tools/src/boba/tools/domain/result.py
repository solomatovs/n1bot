"""ToolResult sealed-семейство: TextResult | JsonResult | ErrorResult.

`ToolResult` экспортируется как type alias на discriminated union
(`Annotated[TextResult | JsonResult | ErrorResult, Field(discriminator="kind")]`).
Это даёт строгую типизацию Pydantic-полей и нативный JSON-Schema `oneOf`.

Открытость для нового варианта: добавить класс `ToolResultBase`-подкласс,
прописать `kind: Literal["..."]`, расширить union в `ToolResult`. Все
существующие потребители используют `match`-выражения с discriminator —
pyright принудит дописать ветку.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorResult",
    "JsonResult",
    "TextResult",
    "ToolResult",
    "ToolResultBase",
]


class ToolResultBase(BaseModel, ABC):
    """Базовый Pydantic-класс для наследования конкретных ToolResult-вариантов.

    Не для использования как тип значения — используй `ToolResult` (alias
    на discriminated union). Этот класс публичен только для наследования
    при добавлении нового варианта в sealed-семейство.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class TextResult(ToolResultBase):
    """Простой текст. Используется tools без структурированного payload'а."""

    kind: Literal["text"] = "text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


class JsonResult(ToolResultBase):
    """JSON-сериализуемый payload."""

    kind: Literal["json"] = "json"
    payload: Any
    metadata: Mapping[str, str] = Field(default_factory=dict)


class ErrorResult(ToolResultBase):
    """Tool не выполнен: ошибка домена, отклонение guard'а, невалидные args.

    Отдельный sealed-вариант, чтобы провайдер мог рендерить ошибки иначе
    (например, для Anthropic — отдельный `is_error: true` блок).
    """

    kind: Literal["error"] = "error"
    message: str
    error_kind: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


ToolResult: TypeAlias = Annotated[
    TextResult | JsonResult | ErrorResult, Field(discriminator="kind"),
]
"""Тип значения tool-результата: discriminated union по полю `kind`.

Используй для типизации полей моделей, возвращаемых значений функций и
параметров. Pydantic генерирует `oneOf` JSON Schema из коробки.
"""
