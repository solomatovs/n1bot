"""ConfigSection: декларация секции конфига (id + namespace + schema)."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar, Generic, TypeVar

from boba.config.path import ConfigPath, NameSegment
from boba.declaration import ObjectSchema
from boba.patterns import StrId

__all__ = ["ConfigSection"]


T = TypeVar("T")


class ConfigSection(ABC, Generic[T]):
    """Декларация одной секции конфига приложения.

    Подкласс задаёт:
      * `namespace` — путь в плоском конфиге (например `("agent",)` →
                      ConfigPath `$agent`);
      * `schema`    — `ObjectSchema[T]` с полями секции;
      * `id`        — опциональный StrId секции (ключ в AppConfig._sections).
                      Если не задан, выводится как `StrId(".".join(namespace))`.
                      Явно объявляется только когда нужен другой ключ
                      (alias или две секции на один namespace).

    Сборка DTO выполняется через AppConfigFactory:
      `Materializer(schema).materialize(flat, ConfigPath.of(NameSegment(*ns)))`.
    """

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    schema: ClassVar[ObjectSchema]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "id" not in cls.__dict__ and "namespace" in cls.__dict__:
            cls.id = StrId(".".join(cls.namespace))

    def prefix(self) -> ConfigPath:
        """ConfigPath, под которым лежат поля секции в FlatConfig."""
        return ConfigPath.of(*(NameSegment(p) for p in self.namespace))
