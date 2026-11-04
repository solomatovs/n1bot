"""Порты запуска плагинов и выбор зарегистрированной фабрики по конфигу.

Ошибки:
RuntimeError — запуск не настроен или программа плагина отказала.
ConfigError — неизвестный либо некорректный способ запуска.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel

from boba.config import ConfigError
from boba.core.components import ComponentConfigError, Components
from boba.identity.context import CallContexts
from boba.tool_io.chain import CallAmbient
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import PluginManifest


@dataclass(frozen=True)
class PluginLaunch:
    """Что способу запуска нужно от плагина: его манифест и секция [tool.<id>]
    значениями JSON. Собирает его загрузчик плагинов после чтения манифеста."""

    manifest: PluginManifest
    section: Mapping[str, Any]

    @property
    def id(self) -> str:
        return self.manifest.id


class SectionLaunchers(Protocol):
    """Способ запуска плагинов: проверки старта, подкоманды программы
    плагина и исполнители его вызовов."""

    @abstractmethod
    def probe(self) -> None:
        """Проверяет предпосылки способа запуска; нарушение — отказ старта."""

    @abstractmethod
    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        """Манифест плагина от его программы: подкоманда manifest с секцией
        section на stdin."""

    @abstractmethod
    def migration_of(
        self,
        launch: PluginLaunch,
        section: Mapping[str, Any],
        version: str | None = None,
    ) -> str:
        """План SQL без подключения к БД и без передачи учётных данных в ответе."""

    @abstractmethod
    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        """Исполнитель вызовов плагина; его изоляция проверяется здесь.
        contexts — держатель контекста вызова: по нему исполнитель узнаёт
        пользователя и область на момент вызова."""

    @abstractmethod
    def settle(self) -> None:
        """Дожидается подъёма исполнителей, начатого launcher_of: сборка
        инструментов зовёт его после всех плагинов, неудача — отказ старта."""

    @abstractmethod
    def stop(self) -> None:
        """Гасит всё, что способ запуска держит между вызовами; зовётся на
        остановке процесса."""


class NoLaunchers(SectionLaunchers):
    """Реализация SectionLaunchers процесса, который плагинов инструментов не
    исполняет: в его конфиге нет секции [tool_launcher]. Так собран чат —
    его инструменты исполняет сервис boba-mcp. Плагин в конфиге такого
    процесса получает отказ на сборке реестра."""

    def probe(self) -> None:
        return

    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        raise RuntimeError(self._refusal(plugin_id))

    def migration_of(
        self,
        launch: PluginLaunch,
        section: Mapping[str, Any],
        version: str | None = None,
    ) -> str:
        raise RuntimeError(self._refusal(launch.id))

    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        raise RuntimeError(self._refusal(launch.id))

    def settle(self) -> None:
        return

    def stop(self) -> None:
        return

    @staticmethod
    def _refusal(plugin_id: str) -> str:
        return (
            f"tool plugin {plugin_id!r} cannot be launched: the config has no "
            "[tool_launcher] section with a registered provider"
        )


class ToolLaunchers:
    """Разбирает секцию запуска фабриками, явно подключёнными приложением."""

    SECTION: ClassVar[str] = "tool_launcher"

    def __init__(
        self,
        raw: DictConfig,
        backends: Components[BaseModel, DictConfig, SectionLaunchers],
    ) -> None:
        self._raw = raw
        self._backends = backends

    def build(self) -> SectionLaunchers:
        node = OmegaConf.select(self._raw, self.SECTION)
        if node is None:
            return NoLaunchers()

        if not isinstance(node, DictConfig):
            raise ConfigError("[tool_launcher] must be a table")

        try:
            values = OmegaConf.to_container(node, resolve=True)
            config = self._backends.parse(values)
            return self._backends.build(config, self._raw)
        except ComponentConfigError as exc:
            raise ConfigError(f"[tool_launcher]: {exc}") from exc
