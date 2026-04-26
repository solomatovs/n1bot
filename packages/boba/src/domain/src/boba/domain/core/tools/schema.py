"""Описание инструмента: идентификаторы и схема параметров.

Содержит data-объекты, которые tool-автор заполняет в :meth:`Tool.definition`:
:class:`ParamSchema`, :class:`ToolInputSchema`, :class:`ToolDefinition`.
Wire-схема (:class:`~boba.domain.core.schema.ParamWireSchema` +
:class:`~boba.domain.core.schema.SchemaContributor`) живёт в
:mod:`boba.domain.core.schema` — общий контракт для tools и config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from boba.domain.core.patterns import Converter, Id
from boba.domain.core.schema import ParamWireSchema, SchemaContributor


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
class ParamSchema:
    """Описание одного параметра инструмента.

    Все три поля обязательны — сознательно строгая схема: автор tool'а
    обязан явно задать имя, человекочитаемое описание и валидатор
    (даже ``Pass`` для no-op случая). Тип, ``required``, ``default``,
    ``enum`` и прочие schema-аспекты выводятся из ``validator`` через
    :class:`SchemaContributor` — единый источник правды для
    runtime-валидации и wire-схемы.
    """

    name: str
    description: str
    converter: Converter[Any, Any]

    def build_wire_schema(self) -> ParamWireSchema:
        """Собрать wire-описание этого параметра, обходя конвертер.

        Стартует с ``description`` и даёт конвертеру дозаполнить
        ``type``/``enum``/``default``/``required`` через
        :meth:`SchemaContributor.contribute`. Если конвертер не
        реализует contributor-протокол — вернёт минимум: только
        описание.
        """
        schema = ParamWireSchema(property={"description": self.description})
        if isinstance(self.converter, SchemaContributor):
            self.converter.contribute(schema)
        return schema


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента.

    ``params`` — независимые описания каждого параметра (валидация +
    описание для LLM).

    ``invariants`` — cross-field конвертер, работающий над dict'ом
    уже провалидированных параметров. Проверяет инварианты, связывающие
    несколько полей: взаимоисключения, совместность, порядок. Обязателен
    даже при отсутствии таких связей — тогда передавай ``Pass``.
    """

    params: list[ParamSchema]
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
