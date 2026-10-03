"""Инструменты процесса чата: таблица плагинов, обвязки поверхности и
собственные инструменты.

Таблица — общая для процессов (entry points установленных пакетов); чат
добавляет поверх тел обвязку ChatMount, монтирующую элементы результата в
ленту и панель. Свои инструменты чата — каталог соединений пользователя:
соединения хранит чат, поэтому это его собственный сервер инструментов.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig

from boba.access import GrantCheck
from boba.chainlit.rendering.mount import ChatMount
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import StoreRef
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

    @staticmethod
    def own_tools(store_ref: StoreRef) -> Sequence[BaseTool]:
        """Собственный сервер инструментов чата: каталог соединений."""
        return ConnectionTools(store_ref).build()

    @classmethod
    def load(cls, raw_config: DictConfig, refs: RuntimeRefs) -> ToolRegistry:
        loader = ToolLoader(
            raw_config,
            CoreTools.table(),
            refs,
            GrantCheck.STRICT,
            cls.surface_hooks(),
            cls.own_tools(refs.connection_store),
        )

        return loader.load()
