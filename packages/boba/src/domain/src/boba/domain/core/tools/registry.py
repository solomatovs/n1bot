"""Реестр и диспетчер вызовов tool'ов."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import Any

from boba.domain.core.declaration import ObjectSchema
from boba.domain.core.patterns import (
    Executor,
    FoldFactory,
    PrioritySource,
    Specification,
)
from boba.domain.core.tools.errors import ToolExecutionError, ToolIdCollisionError
from boba.domain.core.tools.ids import ToolId, ToolSourceId
from boba.domain.core.tools.tool import Tool, ToolCall, ToolContext, ToolResult


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

    def definitions(self) -> Iterable[ObjectSchema[dict[str, Any]]]:
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


class StaticToolSource(ToolSource):
    """Фиксированный набор Tool, зашитый в код."""

    def __init__(
        self,
        source_id: ToolSourceId,
        priority: int,
        tools: Iterable[Tool[Any]],
    ) -> None:
        self._id = source_id
        self._priority = priority
        self._tools = list(tools)

    def id(self) -> ToolSourceId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._tools)


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


class ToolsService(Executor[ToolContext, ToolCall, ToolResult]):
    """Диспетчер tool-вызовов над ToolCatalog; ошибки → ToolExecutionError."""

    def __init__(
        self,
        factory: ToolFactory,
    ) -> None:
        self._factory = factory
        self._catalog: ToolCatalog = ToolCatalog([])

    def rebuild_catalog(self) -> None:
        """Пересобрать каталог из источников фабрики."""
        self._catalog = self._factory.build()

    def filter(self, spec: Specification[Tool[Any]]) -> None:
        """Сжать каталог по спецификации; не сбрасывает фабрику."""
        self._catalog = ToolCatalog(t for t in self._catalog.tools() if spec.check(t))

    def tools(self) -> Iterable[Tool[Any]]:
        """Все собранные инструменты — если нужны и id, и definition."""
        return self._catalog.tools()

    def definitions(self) -> Iterable[ObjectSchema[dict[str, Any]]]:
        """Описания всех собранных инструментов — для передачи потребителю."""
        return self._catalog.definitions()

    def execute(self, ctx: ToolContext, req: ToolCall) -> ToolResult:
        tool = self._catalog.get(req.tool_id)
        if tool is None:
            raise self._unknown_tool(req.tool_id)
        try:
            args = tool.args_converter().convert(req.arguments)
            return tool.execute(ctx, args)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(
                tool_id=req.tool_id,
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
