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
from boba.identity.context import CallContexts
from boba.runtime.plugins import EntryPointPlugins, ToolLoader
from boba.runtime.refs import RuntimeRefs
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.wrapping import CallHooks

__all__ = ["ChatPlugins"]


class ChatPlugins:
    """Загрузка реестра инструментов чата с обвязками его поверхности.

    Создаётся сборкой чата из держателя контекста вызова процесса: его
    получают обвязка элементов результата и инструменты каталога.
    """

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts

    def surface_hooks(self) -> Sequence[CallHooks[Any]]:
        return (ChatMount(self._contexts),)

    def own_tools(self, store_ref: StoreRef) -> Sequence[BaseTool]:
        """Собственный сервер инструментов чата: каталог соединений."""
        return ConnectionTools(store_ref, self._contexts).build()

    def load(self, raw_config: DictConfig, refs: RuntimeRefs) -> ToolRegistry:
        loader = ToolLoader(
            raw_config,
            EntryPointPlugins.discover(),
            refs,
            GrantCheck.STRICT,
            self.surface_hooks(),
            self.own_tools(refs.connection_store),
        )

        return loader.load()
