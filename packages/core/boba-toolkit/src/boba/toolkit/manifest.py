"""Манифест tool-плагина: что пакет инструментов объявляет entry point'ом.

Пакет публикует объект манифеста в группе entry points `boba.tools`; приложение
обнаруживает установленные пакеты через importlib.metadata и собирает таблицу
плагинов без перечисления в коде.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.toolkit.entry import ToolLike

__all__ = ["LaunchSpec", "ToolPluginManifest"]


@dataclass(frozen=True)
class ToolPluginManifest:
    """Объявление плагина пакетом инструментов.

    section — идентификатор плагина: имя секции tool.<section> и имя файла
    конфига conf/plugins/<section>.toml.
    """

    GROUP: ClassVar[str] = "boba.tools"
    """Группа entry points, в которой пакеты публикуют манифесты."""

    section: str
    tools: tuple[ToolLike, ...] = ()


@dataclass(frozen=True)
class LaunchSpec:
    """Что секции нужно от способа запуска: имя, модули тел, пакет и изоляция.

    package — дистрибутив entry point'а: по нему ищется образ корня
    plugins/<package>/rootfs.ext4.
    """

    section: str
    modules: tuple[str, ...] = ()
    package: str = ""
