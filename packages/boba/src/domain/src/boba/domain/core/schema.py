"""Wire-схемы валидируемых значений: контракт «converter → JSON-Schema».

Семья wire-описаний:

- ParamWireSchema — описание одного поля, накапливаемое
  SchemaContributor-конвертерами.
- ObjectWireSchema — JSON-Schema-подобное описание агрегата
  (объекта/секции): description + properties + required.

Изначально жил в schema; вынесен в core,
поскольку контракт не специфичен для tool-параметров — те же
конвертеры описывают и поля FieldSpec,
а агрегат — это любой ObjectSchema,
будь то tool-input или config-section.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ObjectWireSchema",
    "ParamWireSchema",
    "SchemaContributor",
]


@dataclass
class ParamWireSchema:
    """Wire-описание одного параметра/поля, собираемое из конвертеров.

    property — JSON-Schema-подобный dict (type, description,
    enum, default, ...). Конвертер потребителя (OpenAI, Anthropic,
    operator-docs renderer) забирает его как есть.

    required — флаг «параметр обязателен»; конвертер кладёт имя
    в top-level массив required.
    """

    property: dict[str, Any] = field(default_factory=dict)
    required: bool = False


@dataclass
class ObjectWireSchema:
    """JSON-Schema-подобное описание объекта.

    Аналог ParamWireSchema, но для агрегата: description —
    описание самого объекта/секции, properties — словарь wire-описаний
    каждого поля, required — имена обязательных полей.
    Сериализуется в OpenAI tool function schema или в operator-доку
    конфига одним и тем же кодом.
    """

    description: str = ""
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


class SchemaContributor(ABC):
    """Mixin: конвертер умеет дополнять ParamWireSchema.

    Реализуется теми конвертерами, чьё правило отражается в JSON-Schema.
    Композитные конвертеры (например, ChainConverter) делегируют
    contribute всем участникам цепочки, реализующим этот контракт.
    """

    @abstractmethod
    def contribute(self, schema: ParamWireSchema) -> None:
        """Дополнить schema данными, выводимыми из этого конвертера."""
        ...
