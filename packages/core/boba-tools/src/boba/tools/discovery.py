"""Entry-points discovery плагинов.

Плагин — это Python-модуль с `@tool`/`@provides`-декорированными
объектами. В `pyproject.toml` плагин-пакета объявляется через
entry-point group `boba.plugins`:

    [project.entry-points."boba.plugins"]
    chromadb = "boba.tool.chromadb"

Value entry-point'а — путь к модулю (не к классу). `ep.load()`
импортирует модуль и возвращает его — этот объект
`AgentBuilder.use_plugin(...)` принимает как есть.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable

__all__ = ["DEFAULT_PLUGIN_GROUP", "discover_plugins"]

logger = logging.getLogger(__name__)


DEFAULT_PLUGIN_GROUP: str = "boba.plugins"
"""Default entry-point group для плагинов."""


def discover_plugins(
    group: str = DEFAULT_PLUGIN_GROUP,
) -> Iterable[object]:
    """Загрузить плагины (модули) из указанной entry-point group.

    Плохо загружающиеся / некорректные entry-points логируются и
    пропускаются — discovery не должна валить старт приложения.
    """
    for ep in importlib.metadata.entry_points(group=group):
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plugin entry-point %r load failed: %s: %s; skipped",
                ep.name,
                type(exc).__name__,
                exc,
            )
            continue
        yield obj
