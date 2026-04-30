"""ConfigBundle / ConfigFactory: публичный фасад поверх FlatConfig + materialize."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from boba.domain.core.confignext.declaration import ObjectSchema
from boba.domain.core.confignext.flat import FlatConfig, FlatConfigBuilder
from boba.domain.core.confignext.lookup import ConfigLookup
from boba.domain.core.confignext.path import ConfigPath
from boba.domain.core.confignext.source import ConfigSource
from boba.domain.core.confignext.value import ConfigValue

__all__ = ["ConfigBundle", "ConfigFactory"]


T = TypeVar("T")


@dataclass(frozen=True)
class ConfigBundle:
    """Публичный фасад над собранным FlatConfig.

    Способы использования:
      - bundle.materialize(SCHEMA, ConfigPath.parse("$ext.chromadb")) → DTO.
      - bundle.subtree(ConfigPath.parse("$ext.chromadb")) → плоский срез под префиксом.
      - bundle.lookup(ConfigPath.parse("$agent.max_iterations")) → ConfigLookup.
    """

    flat: FlatConfig

    def materialize(
        self,
        schema: ObjectSchema[T],
        prefix: ConfigPath,
    ) -> T:
        return schema.materialize(self.flat, prefix)

    def subtree(self, prefix: ConfigPath) -> Mapping[ConfigPath, ConfigValue]:
        return self.flat.subtree(prefix)

    def lookup(self, path: ConfigPath) -> ConfigLookup[ConfigValue]:
        return self.flat.lookup(path)

    def origin_of(self, path: ConfigPath) -> ConfigLookup[str]:
        return self.flat.origin_of(path)


class ConfigFactory:
    """Сборщик ConfigBundle из набора источников."""

    def __init__(self) -> None:
        self._sources: list[ConfigSource] = []

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        self._sources.extend(sources)

    def build(self) -> ConfigBundle:
        return ConfigBundle(flat=FlatConfigBuilder.from_sources(self._sources))
