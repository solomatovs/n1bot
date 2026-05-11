"""In-memory ConfigSource поверх готового {ConfigPath: ConfigValue}."""

from __future__ import annotations

from collections.abc import Mapping

from boba.config.path import ConfigPath, ConfigSource
from boba.schema.value import ScalarValue


class DictSource(ConfigSource):
    """In-memory ConfigSource поверх готового {ConfigPath: ConfigValue}."""

    def __init__(
        self,
        values: Mapping[ConfigPath, ScalarValue],
        *,
        name: str = "dict",
        priority: int = 100,
    ) -> None:
        self._values = dict(values)
        self._name = name
        self._priority = priority

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ScalarValue]:
        return dict(self._values)
