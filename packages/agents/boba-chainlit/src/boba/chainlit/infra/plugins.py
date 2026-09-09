"""Инструменты процесса чата: таблица плагинов и обвязки поверхности.

Таблица — общая для процессов (entry points установленных пакетов); чат
добавляет поверх тел обвязку ChatMount, монтирующую элементы результата в
ленту и панель.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omegaconf import DictConfig

from boba.access import GrantCheck
from boba.chainlit.rendering.mount import ChatMount
from boba.runtime.plugins import CoreTools, ToolLoader
from boba.runtime.refs import RuntimeRefs
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.wrapping import CallHooks

__all__ = ["ChatPlugins"]


class ChatPlugins:
    """Загрузка реестра инструментов чата с обвязками его поверхности."""

    @staticmethod
    def surface_hooks() -> Sequence[CallHooks[Any]]:
        return (ChatMount(),)

    @classmethod
    def load(cls, raw_config: DictConfig, refs: RuntimeRefs) -> ToolRegistry:
        loader = ToolLoader(
            raw_config,
            CoreTools.table(),
            refs,
            GrantCheck.STRICT,
            cls.surface_hooks(),
        )

        return loader.load()
