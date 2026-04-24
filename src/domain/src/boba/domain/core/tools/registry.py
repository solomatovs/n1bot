"""Реестр и диспетчер вызовов tool'ов.

:class:`ToolStore` и :class:`ToolCatalog` — мутабельный/иммутабельный
лукапы по :class:`ToolId`. :class:`ToolSource` — стадия наполнения
store'а одного источника. :class:`ToolFactory` — :class:`FoldFactory`,
собирающий каталог из всех источников. :class:`ToolsService` —
исполнитель ``ToolCall → ToolResult`` поверх каталога.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import Any

from boba.domain.core.patterns import Executor, FoldFactory, PrioritySource
from boba.domain.core.tools.errors import ToolExecutionError, ToolIdCollisionError
from boba.domain.core.tools.schema import (
    ToolDefinition,
    ToolId,
    ToolSourceId,
)
from boba.domain.core.tools.tool import Tool, ToolCall, ToolResult


class ToolStore:
    def __init__(self) -> None:
        self._items: dict[ToolId, Tool[Any]] = {}

    def get(self, tool_id: ToolId) -> Tool[Any] | None:
        return self._items.get(tool_id)

    def add(self, tool: Tool[Any]) -> None:
        self._items[tool.tool_id()] = tool

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._items.values())


class ToolCatalog:
    def __init__(self, tools: Iterable[Tool[Any]]) -> None:
        self._items: dict[ToolId, Tool[Any]] = {tool.tool_id(): tool for tool in tools}

    def get(self, tool_id: ToolId) -> Tool[Any] | None:
        return self._items.get(tool_id)

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._items

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._items.values())

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех инструментов — для передачи потребителю."""
        return (tool.definition() for tool in self._items.values())


class ToolSource(
    PrioritySource[ToolSourceId, ToolStore],
):
    @abstractmethod
    def tools(self) -> Iterable[Tool[Any]]: ...

    def apply(self, state: ToolStore) -> ToolStore:
        for tool in self.tools():
            tool_id = tool.tool_id()
            existing = state.get(tool_id)

            if existing:
                raise ToolIdCollisionError(
                    tool_id,
                    existing.tool_source_id(),
                    tool.tool_source_id(),
                )

            state.add(tool)

        return state


class ToolFactory(
    FoldFactory[
        ToolSourceId,
        ToolStore,
        ToolCatalog,
    ],
):
    def initial(self) -> ToolStore:
        return ToolStore()

    def finalize(self, state: ToolStore) -> ToolCatalog:
        return ToolCatalog(state.tools())


class ToolsService(Executor[None, ToolCall, ToolResult]):
    """Диспетчер tool-вызовов над :class:`ToolCatalog`.

    Маршрутизация встроена по ``call.tool_id``: ищет :class:`Tool` в
    ToolCatalog и вызывает ``tool.execute``.

    Ошибка :class:`ToolExecutionError`:
    - неизвестный tool;
    - ошибка парсинга/валидации аргументов;
    - произвольное исключение тула.
    """

    def __init__(
        self,
        factory: ToolFactory,
    ) -> None:
        self._factory = factory
        self._catalog: ToolCatalog = ToolCatalog([])

    def rebuild_catalog(self) -> None:
        """Пересобрать каталог из источников фабрики."""
        self._catalog = self._factory.build()

    def tools(self) -> Iterable[Tool[Any]]:
        """Все собранные инструменты — если нужны и id, и definition."""
        return self._catalog.tools()

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех собранных инструментов — для передачи потребителю."""
        return self._catalog.definitions()

    def execute(self, ctx: None, call: ToolCall) -> ToolResult:
        tool = self._catalog.get(call.tool_id)
        if tool is None:
            raise self._unknown_tool(call.tool_id)
        try:
            args = tool.args_converter().convert(call.arguments)
            return tool.execute(ctx, args)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(
                tool_id=call.tool_id,
                message=f"{type(e).__name__}: {e}",
            ) from e

    def _unknown_tool(self, tool_id: ToolId) -> ToolExecutionError:
        available = [t.tool_id().to_wire() for t in self._catalog.tools()]
        if not available:
            msg = f"tool {tool_id.to_wire()!r} not found; no tools are registered"
        else:
            msg = (
                f"tool {tool_id.to_wire()!r} not found. "
                f"available: {', '.join(repr(a) for a in available)}"
            )
        return ToolExecutionError(tool_id=tool_id, message=msg)
