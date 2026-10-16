"""Манифест tool-плагина: что пакет инструментов объявляет entry point'ом.

Пакет публикует объект манифеста в группе entry points `boba.tools`; приложение
обнаруживает установленные пакеты через importlib.metadata и собирает таблицу
плагинов без перечисления в коде. Плагин с хранилищем объявляет его стартом
(PluginStart): процесс boba-mcp на своём старте создаёт таблицы плагина.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from pydantic import BaseModel

from boba.toolkit.facade import PayloadTool

__all__ = ["LaunchSpec", "PluginStart", "PluginStorage", "ToolPluginManifest"]

CfgT = TypeVar("CfgT", bound=BaseModel)


class PluginStorage(Protocol):
    """Хранилище плагина: setup создаёт его таблицы при старте плагина в
    процессе boba-mcp; повтор безвреден. Реализуют KbSchema и DescriberStore."""

    @abstractmethod
    async def setup(self) -> None: ...


@dataclass(frozen=True)
class PluginStart(Generic[CfgT]):
    """Старт плагина с хранилищем: модель его секции tool.<section> и
    хранилище по ней. Объявляется в манифесте; boba-mcp на своём старте
    разбирает секцию включённого плагина в config и зовёт storage(cfg).setup().
    """

    config: type[CfgT]
    storage: Callable[[CfgT], PluginStorage]


@dataclass(frozen=True)
class ToolPluginManifest:
    """Объявление плагина пакетом инструментов.

    section — идентификатор плагина: имя секции tool.<section> и имя файла
    конфига conf/plugins/<section>.toml.
    """

    GROUP: ClassVar[str] = "boba.tools"
    """Группа entry points, в которой пакеты публикуют манифесты."""

    section: str
    tools: tuple[PayloadTool, ...] = ()
    start: PluginStart[Any] | None = None
    """Хранилище плагина, которое boba-mcp готовит на своём старте; None —
    у плагина своих таблиц нет."""


@dataclass(frozen=True)
class LaunchSpec:
    """Что секции нужно от способа запуска: имя, модули тел, пакет и изоляция.

    package — дистрибутив entry point'а: по нему ищется образ корня
    plugins/<package>/rootfs.ext4.
    """

    section: str
    modules: tuple[str, ...] = ()
    package: str = ""
