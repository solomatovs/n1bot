"""ConfigSection: декларация секции конфига (id + namespace + schema)."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar, Generic, TypeVar

from boba_patterns import StrId

from boba_next.config.path import ConfigPath, NameSegment
from boba_next.declaration import ObjectSchema

__all__ = ["ConfigSection"]


T = TypeVar("T")


class ConfigSection(ABC, Generic[T]):
    """Декларация одной секции конфига приложения.

    Подкласс задаёт три атрибута:
      * `id`        — уникальный StrId секции (для AppConfig.section);
      * `namespace` — путь в плоском конфиге (например `("agent",)` →
                      ConfigPath `$agent`);
      * `schema`    — `ObjectSchema[T]` с полями секции.

    Сборка DTO выполняется через AppConfigFactory:
      `Materializer(schema).materialize(flat, ConfigPath.of(NameSegment(*ns)))`.
    """

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    schema: ClassVar[ObjectSchema]

    def prefix(self) -> ConfigPath:
        """ConfigPath, под которым лежат поля секции в FlatConfig."""
        return ConfigPath.of(*(NameSegment(p) for p in self.namespace))
