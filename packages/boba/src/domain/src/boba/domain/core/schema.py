"""Wire-схема валидируемых значений: контракт «валидатор → JSON-Schema».

:class:`ParamWireSchema` — JSON-Schema-подобный dict, накапливаемый
:class:`SchemaContributor`-валидаторами. Используется потребителями
(LLM-провайдеры для tools, операторская дока для config), которым нужно
не само runtime-правило, а его описание.

Изначально жил в :mod:`boba.domain.core.tools.schema`; вынесен в core,
поскольку контракт не специфичен для tool-параметров — те же валидаторы
описывают и поля :class:`~boba.domain.core.config.FieldSpec`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ParamWireSchema",
    "SchemaContributor",
]


@dataclass
class ParamWireSchema:
    """Wire-описание одного параметра/поля, собираемое из валидаторов.

    ``property`` — JSON-Schema-подобный dict (``type``, ``description``,
    ``enum``, ``default``, ...). Конвертер потребителя (OpenAI, Anthropic,
    operator-docs renderer) забирает его как есть.

    ``required`` — флаг «параметр обязателен»; конвертер кладёт имя
    в top-level массив ``required``.
    """

    property: dict[str, Any] = field(default_factory=dict)
    required: bool = False


class SchemaContributor(ABC):
    """Mixin: валидатор умеет дополнять :class:`ParamWireSchema`.

    Реализуется теми валидаторами, чьё правило отражается в JSON-Schema.
    Композитные валидаторы (например, ChainValidator) делегируют
    contribute всем участникам цепочки, реализующим этот контракт.
    """

    @abstractmethod
    def contribute(self, schema: ParamWireSchema) -> None:
        """Дополнить ``schema`` данными, выводимыми из этого валидатора."""
        ...
