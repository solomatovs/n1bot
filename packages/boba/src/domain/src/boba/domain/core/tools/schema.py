"""Описание инструмента: идентификаторы и схема параметров.

Содержит data-объекты, которые tool-автор заполняет в :meth:`Tool.definition`:
:class:`ParamSchema`, :class:`ToolInputSchema`, :class:`ToolDefinition`.
Плюс :class:`ParamWireSchema` + :class:`SchemaContributor` — контракт, по
которому валидатор параметра контрибьютит в wire-схему для LLM.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Self

from boba.domain.core.patterns import Id, Validator


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


@dataclass
class ParamWireSchema:
    """Wire-описание одного параметра, собираемое из валидаторов.

    ``property`` — JSON-Schema-подобный dict (``type``, ``description``,
    ``enum``, ``default``, ...). Конвертер провайдера (OpenAI, Anthropic)
    забирает его как есть.

    ``required`` — флаг «параметр обязателен»; конвертер кладёт имя
    в top-level массив ``required``.
    """

    property: dict[str, Any] = field(default_factory=dict)
    required: bool = False


class SchemaContributor(ABC):
    """Mixin: валидатор умеет дополнять :class:`ParamWireSchema`.

    Реализуется теми валидаторами, чьё правило отражается в JSON-Schema.
    Композитные валидаторы (ChainValidator) делегируют contribute всем
    участникам цепочки, реализующим этот контракт.
    """

    @abstractmethod
    def contribute(self, schema: ParamWireSchema) -> None:
        """Дополнить ``schema`` данными, выводимыми из этого валидатора."""
        ...


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
    validator: Validator[Any]

    def build_wire_schema(self) -> ParamWireSchema:
        """Собрать wire-описание этого параметра, обходя валидатор.

        Стартует с ``description`` и даёт валидатору дозаполнить
        ``type``/``enum``/``default``/``required`` через
        :meth:`SchemaContributor.contribute`. Если валидатор не
        реализует contributor-протокол — вернёт минимум: только
        описание.
        """
        schema = ParamWireSchema(property={"description": self.description})
        if isinstance(self.validator, SchemaContributor):
            self.validator.contribute(schema)
        return schema


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента.

    ``params`` — независимые описания каждого параметра (валидация +
    описание для LLM).

    ``invariants`` — cross-field валидатор, работающий над dict'ом
    уже провалидированных параметров. Проверяет инварианты, связывающие
    несколько полей: взаимоисключения, совместность, порядок. Обязателен
    даже при отсутствии таких связей — тогда передавай ``Pass``.
    """

    params: list[ParamSchema]
    invariants: Validator[dict[str, Any]]


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
