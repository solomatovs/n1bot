"""Манифест tool-плагина: что пакет инструментов объявляет entry point'ом.

Пакет публикует объект манифеста в группе entry points `boba.tools`; приложение
обнаруживает установленные пакеты через importlib.metadata и собирает таблицу
плагинов без перечисления в коде.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

from boba.toolkit.entry import ToolLike
from boba.toolkit.launcher import LauncherFactory

__all__ = ["ToolPluginManifest"]

ManifestBuild = Callable[[Any, LauncherFactory], Sequence[ToolLike]]
"""Фабрика инструментов секции: конфиг секции и исполнители -> инструменты."""


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
    config_model: type[BaseModel] | None = None
    build: ManifestBuild | None = None
