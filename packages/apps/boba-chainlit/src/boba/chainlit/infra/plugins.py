"""Собственные инструменты процесса чата: каталог соединений пользователя.

Инструменты исполняет сервис boba-mcp; у чата остаётся то, что завязано на
его собственные данные и права: соединения хранит чат, поэтому каталог
соединений — его собственный сервер инструментов. Плагинов инструментов чат
не загружает.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.tools import BaseTool

from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import StoreRef
from boba.identity.context import CallContexts
from boba.runtime.plugins import ToolPlugin

__all__ = ["ChatPlugins"]


class ChatPlugins:
    """Инструменты, которые чат исполняет сам.

    Создаётся сборкой чата из держателя контекста вызова процесса и отдаёт
    сборке реестра его входы: собственные инструменты и таблицу плагинов.
    """

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts

    def own_tools(self, store_ref: StoreRef) -> Sequence[BaseTool]:
        """Собственный сервер инструментов чата: каталог соединений."""
        return ConnectionTools(store_ref, self._contexts).build()

    def table(self) -> Mapping[str, ToolPlugin]:
        """Таблица плагинов чата: пустая, плагины исполняет сервис."""
        return {}
