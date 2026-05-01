"""Wire-схемы значений: contract converter → JSON-Schema (локальная копия)."""

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
    """Wire-описание одного параметра/поля."""

    property: dict[str, Any] = field(default_factory=dict)
    required: bool = False


@dataclass
class ObjectWireSchema:
    """JSON-Schema-подобное описание объекта."""

    description: str = ""
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


class SchemaContributor(ABC):
    """Mixin: конвертер умеет дополнять ParamWireSchema."""

    @abstractmethod
    def contribute(self, schema: ParamWireSchema) -> None:
        """Дополнить schema данными, выводимыми из этого конвертера."""
        ...
