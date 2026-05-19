"""Entry-points discovery v2-плагинов.

V2-плагин — это Python-модуль с `@tool`/`@provides`-декорированными
объектами. В `pyproject.toml` плагин-пакета объявляется через
entry-point group `boba.plugins.v2`:

    [project.entry-points."boba.plugins.v2"]
    chromadb = "boba.tool.chromadb_v2"

Value entry-point'а — путь к модулю (не к классу). `ep.load()` импортирует
модуль и возвращает его — этот объект `ToolKit.add_plugin(...)` принимает
как есть.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable

__all__ = ["DEFAULT_V2_PLUGIN_GROUP", "discover_v2_plugins"]

logger = logging.getLogger(__name__)


DEFAULT_V2_PLUGIN_GROUP: str = "boba.plugins.v2"
"""Default entry-point group для v2-плагинов."""


def discover_v2_plugins(
    group: str = DEFAULT_V2_PLUGIN_GROUP,
) -> Iterable[object]:
    """Загрузить v2-плагины (модули) из указанной entry-point group.

    Плохо загружающиеся / некорректные entry-points логируются и
    пропускаются — discovery не должна валить старт приложения.
    """
    for ep in importlib.metadata.entry_points(group=group):
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "v2 plugin entry-point %r load failed: %s: %s; skipped",
                ep.name,
                type(exc).__name__,
                exc,
            )
            continue
        yield obj
