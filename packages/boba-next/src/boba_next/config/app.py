"""AppConfig (registry) + AppConfigFactory (сборщик секций приложения).

AppConfig — иммутабельный реестр готовых DTO секций по StrId.
AppConfigFactory — регистрирует секции, подключает источники, через
`build()` материализует все секции в один AppConfig.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from boba_patterns import FactoryMethod, StrId

from boba_next.config.bundle import ConfigBundleFactory, FlatConfigMaterializer
from boba_next.config.section import ConfigSection

__all__ = [
    "CONFIG_SECTIONS_ENTRY_POINT",
    "AppConfig",
    "AppConfigFactory",
    "ConfigError",
    "SectionAlreadyRegisteredError",
    "SectionMissingError",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


CONFIG_SECTIONS_ENTRY_POINT = "boba.config_sections"
"""Entry-point group для секций расширений (наследовано от старого ядра)."""


class ConfigError(Exception):
    """Базовая ошибка application-слоя конфига."""


class SectionAlreadyRegisteredError(ConfigError):
    """Секция с таким StrId уже зарегистрирована."""

    def __init__(self, section_id: StrId) -> None:
        super().__init__(f"ConfigSection {section_id!r} is already registered")
        self.section_id = section_id


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
        sid = cls.id
        if sid not in self._sections:
            raise SectionMissingError(cls)
        return cast(T, self._sections[sid])

    def has(self, cls: type[ConfigSection[Any]]) -> bool:
        return cls.id in self._sections


class AppConfigFactory(FactoryMethod[AppConfig]):
    """
    Сборщик секций приложения:
    - регистрирует секции
    - подключает источники
    - материализует все секции в один AppConfig
    """

    def __init__(self, bundle_factory: ConfigBundleFactory) -> None:
        self._bundle_factory = bundle_factory
        self._sections: dict[StrId, ConfigSection[Any]] = {}

    def register(self, section: ConfigSection[Any]) -> None:
        sid = section.id
        if sid in self._sections:
            raise SectionAlreadyRegisteredError(sid)
        self._sections[sid] = section

    def unregister(self, section_id: StrId) -> None:
        self._sections.pop(section_id, None)

    def registered(self) -> tuple[ConfigSection[Any], ...]:
        return tuple(self._sections.values())

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
            try:
                self.register(obj())
            except SectionAlreadyRegisteredError as exc:
                logger.warning(
                    "config-section entry-point %r: %s; skipped",
                    ep.name,
                    exc,
                )

    def build(self) -> AppConfig:
        """Собрать ConfigBundle через bundle_factory и материализовать все секции."""
        bundle = self._bundle_factory.build()
        built: dict[StrId, object] = {}
        for sid, section in self._sections.items():
            built[sid] = FlatConfigMaterializer(section.schema).materialize(
                bundle.flat,
                section.prefix(),
            )
        return AppConfig(built)
