"""Описание инструмента: идентификаторы и схема параметров.

Содержит data-объекты, которые tool-автор заполняет в :meth:`Tool.definition`:
:data:`ToolInputSchema`, :class:`ToolDefinition`. Параметры — это
:class:`~boba.domain.core.config.FieldSpec` (тот же примитив, что
описывает поле конфига); схема — это
:class:`~boba.domain.core.config.ObjectSchema` (тот же примитив, что
описывает секцию конфига). Tool отличается от config только тем, что
сырые данные приходят как ``dict[str, Any]`` от LLM, а не из резолвера.

Wire-схема (:class:`~boba.domain.core.schema.ParamWireSchema`,
:class:`~boba.domain.core.config.ObjectWireSchema` +
:class:`~boba.domain.core.schema.SchemaContributor`) живёт в
:mod:`boba.domain.core.schema` / :mod:`boba.domain.core.config` —
общий контракт для tools и config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self, TypeAlias

from boba.domain.core.config import ObjectSchema
from boba.domain.core.patterns import Id


class ToolId(Id[str]):
    """Уникальный идентификатор инструмента — сквозной ключ поиска и вызова."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class ToolSourceId(Id[str]):
    """Идентификатор источника инструментов (builtin, mcp:server_a, ...)."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


ToolInputSchema: TypeAlias = ObjectSchema[dict[str, Any]]
"""Алиас :class:`ObjectSchema` со специализацией ``factory=dict``.

Tool-args — это всегда ``dict[str, Any]`` (Tool.execute сам типизирует
их через свой ``args_converter``). Используется только как
type-annotation; конструируется через ``ObjectSchema(fields=...,
invariants=..., factory=dict)``.
"""


@dataclass(frozen=True)
class ToolDefinition:
    """Описание инструмента для потребителя: текст и схема параметров.

    ``id`` намеренно не хранится — он живёт на самом Tool
    (:meth:`Tool.tool_id`) и считается источником правды. Запись,
    видимая потребителю, собирается на границе сервиса из пары
    ``(tool.tool_id(), tool.definition())``.
    """

    description: str
    input_schema: ToolInputSchema
