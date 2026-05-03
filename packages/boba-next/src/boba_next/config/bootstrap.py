"""AppConfigBootstrap — one-shot композитор ConfigBundleFactory + AppConfigFactory."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from boba_next.config.app import AppConfig, AppConfigFactory
from boba_next.config.bundle import ConfigBundle, ConfigBundleFactory
from boba_next.config.path import ConfigSource
from boba_next.config.section import ConfigSection

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
        self._app_factory = AppConfigFactory()

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        self._bundle_factory.attach_sources(sources)

    def register_section(self, section: ConfigSection[Any]) -> None:
        self._app_factory.register_section(section)

    def discover_extension_sections(self) -> None:
        self._app_factory.discover_extension_sections()

    def bundle_factory(self) -> ConfigBundleFactory:
        return self._bundle_factory

    def app_factory(self) -> AppConfigFactory:
        return self._app_factory

    def build_bundle(self) -> ConfigBundle:
        """Собрать только foundation-bundle (без секций)."""
        return self._bundle_factory.build()

    def build(self) -> AppConfig:
        """Собрать готовый AppConfig: sources → bundle → секции в реестр."""
        return self._app_factory.build(self._bundle_factory.build())
