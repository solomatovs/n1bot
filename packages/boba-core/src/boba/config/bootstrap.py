"""AppConfigBootstrap — one-shot композитор ConfigBundleFactory + AppConfigFactory."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from boba.config.app import AppConfig, ConfigSectionFactory
from boba.config.bundle import ConfigBundle, ConfigBundleFactory
from boba.config.path import ConfigSource
from boba.config.section import ConfigSection

__all__ = ["AppConfigBootstrap"]


class AppConfigBootstrap:
    """Удобная one-shot обёртка: накопить sources + секций → AppConfig.

    Композирует две foundation-фабрики:
      * `ConfigBundleFactory` — sources → ConfigBundle (FoldFactory).
      * `AppConfigFactory`    — sections → AppConfig (ContextCatalogFactory).

    Использование при старте приложения:

        boot = AppConfigBootstrap()
        boot.attach_sources([toml_source, env_source, cli_source])
        boot.register_section(AppCoreSection())
        boot.register_section(AgentSection())
        boot.discover_extension_sections()
        app: AppConfig = boot.build()
    """

    def __init__(self) -> None:
        self._bundle_factory = ConfigBundleFactory()
        self._section_factory = ConfigSectionFactory()

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        self._bundle_factory.attach_sources(sources)

    def register_section(self, section: ConfigSection[Any]) -> None:
        self._section_factory.register_section(section)

    def discover_extension_sections(self) -> None:
        self._section_factory.discover_extension_sections()

    def bundle_factory(self) -> ConfigBundleFactory:
        return self._bundle_factory

    def app_factory(self) -> ConfigSectionFactory:
        return self._section_factory

    def build_bundle(self) -> ConfigBundle:
        """Собрать только foundation-bundle (без секций)."""
        return self._bundle_factory.build()

    def build(self) -> AppConfig:
        """Собрать готовый AppConfig: sources → bundle → секции в реестр."""
        return self._section_factory.build(self._bundle_factory.build())
