"""Discovery плагинов через entry-points.

`discover_plugins(group)` обходит указанную entry-point group и возвращает
**классы**, удовлетворяющие `Plugin`-протоколу. Конкретный экземпляр
никогда не создаётся — плагин-классы используются как фабрики через
classmethod-ы `config` и `build`.

Точки входа объявляются плагин-пакетами в `pyproject.toml` так:

    [project.entry-points."boba.plugins"]
    confluence_search = "boba.ext.confluence_tools.plugin:ConfluenceSearchPlugin"

Один пакет может декларировать несколько плагинов — каждый отдельной
точкой входа.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable

from boba.plugin import Plugin

__all__ = ["DEFAULT_PLUGIN_ENTRY_POINT_GROUP", "discover_plugins"]

logger = logging.getLogger(__name__)


DEFAULT_PLUGIN_ENTRY_POINT_GROUP: str = "boba.plugins"
"""Имя entry-point group по умолчанию для плагин-классов."""


def discover_plugins(
    group: str = DEFAULT_PLUGIN_ENTRY_POINT_GROUP,
) -> Iterable[type[Plugin]]:
    """Загрузить плагин-классы из указанной entry-point group.

    Плохо загружающиеся / некорректные entry-points логируются и
    пропускаются — discovery не должна валить старт всего приложения.
    """
    for ep in importlib.metadata.entry_points(group=group):
        try:
            obj = ep.load()
        except Exception as exc:
            logger.warning(
                "plugin entry-point %r load failed: %s: %s; skipped",
                ep.name,
                type(exc).__name__,
                exc,
            )
            continue
        if not _looks_like_plugin(obj):
            logger.warning(
                "plugin entry-point %r does not satisfy Plugin protocol: %r; skipped",
                ep.name,
                obj,
            )
            continue
        yield obj


def _looks_like_plugin(obj: object) -> bool:
    """Структурная проверка: класс с NAME, config, build."""
    if not isinstance(obj, type):
        return False
    return (
        hasattr(obj, "NAME")
        and callable(getattr(obj, "config", None))
        and callable(getattr(obj, "build", None))
    )
