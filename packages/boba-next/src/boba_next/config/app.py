"""AppConfig (registry) + AppConfigFactory (сборщик секций приложения).

AppConfigFactory — `ContextCatalogFactory[ConfigBundle, StrId, object, AppConfig]`:
наследует контракт чисто. `build(bundle)` — нативный ContextFactoryMethod, без
override. Двухступенчатая сборка явная на стороне caller'а:

    bundle = bundle_factory.build()
    app    = app_factory.build(bundle)
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from boba.patterns import ContextCatalogFactory, ContextItemProvider, StrId
from boba_next.config.bundle import ConfigBundle, FlatConfigMaterializer
from boba_next.config.section import ConfigSection

__all__ = [
    "CONFIG_SECTIONS_ENTRY_POINT",
    "AppConfig",
    "AppConfigFactory",
    "ConfigError",
    "SectionMissingError",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


CONFIG_SECTIONS_ENTRY_POINT = "boba.config_sections"
"""Entry-point group для секций расширений (наследовано от старого ядра)."""


class ConfigError(Exception):
    """Базовая ошибка application-слоя конфига."""


class SectionMissingError(ConfigError):
    """Секция не зарегистрирована в фабрике."""

    def __init__(self, section_cls: type[ConfigSection[Any]]) -> None:
        super().__init__(
            f"ConfigSection {section_cls.__name__!r} (id={section_cls.id!r}) "
            "is not registered in factory"
        )
        self.section_cls = section_cls


class AppConfig:
    """Иммутабельный реестр готовых DTO секций по StrId."""

    def __init__(self, sections: Mapping[StrId, object]) -> None:
        self._sections: dict[StrId, object] = dict(sections)

    def section(self, cls: type[ConfigSection[T]]) -> T:
        """Достать DTO зарегистрированной секции; иначе SectionMissingError."""
        if cls.id not in self._sections:
            raise SectionMissingError(cls)

        return cast(T, self._sections[cls.id])

    def has(self, cls: type[ConfigSection[Any]]) -> bool:
        return cls.id in self._sections


class _SectionProvider(ContextItemProvider[ConfigBundle, StrId, object]):
    """Адаптер: ConfigSection → ContextItemProvider для AppConfigFactory."""

    def __init__(self, section: ConfigSection[Any]) -> None:
        self._section = section

    @property
    def section(self) -> ConfigSection[Any]:
        return self._section

    def id(self) -> StrId:
        return self._section.id

    def produce(self, ctx: ConfigBundle) -> object:
        return FlatConfigMaterializer(self._section.schema).materialize(
            ctx.flat,
            self._section.prefix(),
        )


class AppConfigFactory(
    ContextCatalogFactory[ConfigBundle, StrId, object, AppConfig],
):
    """Сборщик секций приложения через ContextCatalogFactory.

    Чистое наследование: `build(bundle) → AppConfig` — нативный контракт
    `ContextFactoryMethod[ConfigBundle, AppConfig]`. Повторная регистрация
    секции с тем же id — silent overwrite (last-wins).
    """

    def register_section(self, section: ConfigSection[Any]) -> None:
        """Зарегистрировать секцию (оборачивается в провайдер для CatalogFactory)."""
        self.register(_SectionProvider(section))

    def discover_extension_sections(
        self,
        *,
        group: str = CONFIG_SECTIONS_ENTRY_POINT,
    ) -> None:
        """Подхватить секции через указанную entry-point group."""
        for ep in importlib.metadata.entry_points(group=group):
            try:
                obj = ep.load()
            except Exception as exc:
                logger.warning(
                    "config-section entry-point %r load failed: %s: %s; skipped",
                    ep.name,
                    type(exc).__name__,
                    exc,
                )
                continue
            if not (isinstance(obj, type) and issubclass(obj, ConfigSection)):
                logger.warning(
                    "config-section entry-point %r is not a ConfigSection subclass: "
                    "%r; skipped",
                    ep.name,
                    obj,
                )
                continue
            self.register_section(obj())

    def finalize(
        self,
        ctx: ConfigBundle,
        items: dict[StrId, object],
    ) -> AppConfig:
        # не используется, т.к. секции сами достают из него
        # а фабрика просто собирает DTO в реестр.
        del ctx
        return AppConfig(items)
