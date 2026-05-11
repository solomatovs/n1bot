"""ConfigBundleBuilder — fluent-фасад поверх ConfigBundle.from_sources."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, Self

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigSource
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.patterns import FactoryMethod

__all__ = ["ConfigBundleBuilder"]


class ConfigBundleBuilder(FactoryMethod):
    """Fluent-фасад: накапливает ConfigSource'ы и собирает ConfigBundle."""

    def __init__(self) -> None:
        self._sources: list[ConfigSource] = []

    def use_source(self, source: ConfigSource) -> Self:
        """Зарегистрировать произвольный ConfigSource."""
        self._sources.append(source)
        return self

    def use_sources(self, sources: Iterable[ConfigSource]) -> Self:
        """Зарегистрировать набор ConfigSource'ов."""
        self._sources.extend(sources)
        return self

    def use_cli(self, argv: Sequence[str] | None = None) -> Self:
        """Шорткат: зарегистрировать стандартный CliSource()."""
        return self.use_source(
            CliSource(list(argv)) if argv is not None else CliSource(),
        )

    def use_env(self) -> Self:
        """Шорткат: зарегистрировать стандартный EnvSource()."""
        return self.use_source(EnvSource())

    def use_env_file(self) -> Self:
        """Шорткат: зарегистрировать стандартный EnvFileSource()."""
        return self.use_source(EnvFileSource())

    def pipe(
        self,
        fn: Callable[..., Self],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Self:
        """Extension-style: fn(self, *args, **kwargs) -> Self."""
        return fn(self, *args, **kwargs)

    def build(self) -> ConfigBundle:
        """Собрать ConfigBundle из накопленных source'ов."""
        return ConfigBundle.from_sources(self._sources)
