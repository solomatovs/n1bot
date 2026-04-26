"""Описание инструмента: идентификаторы и схема параметров.

Содержит data-объекты, которые tool-автор заполняет в :meth:`Tool.definition`:
:class:`ToolInputSchema`, :class:`ToolDefinition`. Сами параметры — это
:class:`~boba.domain.core.config.FieldSpec` (тот же примитив, что
описывает поле конфига): унифицировано имя + конвертер + описание +
``build_wire_schema``.

Wire-схема (:class:`~boba.domain.core.schema.ParamWireSchema` +
:class:`~boba.domain.core.schema.SchemaContributor`) живёт в
:mod:`boba.domain.core.schema` — общий контракт для tools и config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from boba.domain.core.config import FieldSpec
from boba.domain.core.patterns import Converter, Id


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


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента.

    ``params`` — независимые описания каждого параметра. Тип параметра —
    :class:`FieldSpec` (тот же примитив, что описывает поле конфига):
    ``name`` — имя ключа в input-dict от LLM, ``converter`` — цепочка
    Required/Default/IsX/constraints, ``description`` — текст для LLM.

    ``invariants`` — cross-field конвертер, работающий над dict'ом
    уже провалидированных параметров. Проверяет инварианты, связывающие
    несколько полей: взаимоисключения, совместность, порядок. Обязателен
    даже при отсутствии таких связей — тогда передавай ``Pass``.
    """

    params: list[FieldSpec[Any]]
    invariants: Converter[dict[str, Any], dict[str, Any]]


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
