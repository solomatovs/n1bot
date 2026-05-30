"""
ToolSource + ToolRegistry + ToolCatalog + ToolExecutor.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Self

from boba.patterns import Executor
from boba.tools.domain.errors import (
    ToolExecutionError,
    ToolIdCollisionError,
    ToolSourceCollisionError,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    parse_tool_id,
)
from boba.tools.domain.tool import (
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSchema,
)

__all__ = [
    "StaticToolSource",
    "ToolCatalog",
    "ToolExecutor",
    "ToolRegistry",
    "ToolSource",
]


class ToolSource:
    """
    Источник Tool'ов: владелец инструментов и связанных ресурсов.

    Один ToolSource содержит много Tool'ов,
    объединённых общими ресурсами (например, клиентом внешнего API)

    Например работа для работы с файлами: cat, grep, ls, wc

    каждый Tool, обязан иметь:
        `id()       - идентификатор источника, совпадающий для всех Tool'ов
        `close()`   — точка освобождения долгоживущих ресурсов;
    """

    @abstractmethod
    def id(self) -> ToolSourceId: ...

    @abstractmethod
    def tools(self) -> Iterable[Tool[Any, Any]]: ...

    @abstractmethod
    def find(self, name: ToolName) -> Tool[Any, Any] | None: ...

    def close(self) -> None:
        """Освободить долгоживущие ресурсы. Default no-op."""


class StaticToolSource(ToolSource):
    """Фиксированный набор Tool'ов, зашитый в код. Без shared-ресурсов."""

    def __init__(
        self,
        source_id: ToolSourceId,
        tools: Iterable[Tool[Any, Any]],
    ) -> None:
        self._id = source_id
        self._index: dict[ToolName, Tool[Any, Any]] = {}
        for tool in tools:
            tid_source, tid_name = parse_tool_id(tool.tool_id())
            if tid_source != source_id:
                msg = (
                    f"tool {tool.tool_id()!r} attached to source "
                    f"{source_id!r} but claims source "
                    f"{tid_source!r}"
                )
                raise ValueError(msg)
            if tid_name in self._index:
                raise ToolIdCollisionError(source_id, tid_name)
            self._index[tid_name] = tool

    def id(self) -> ToolSourceId:
        return self._id

    def tools(self) -> Iterable[Tool[Any, Any]]:
        return iter(self._index.values())

    def find(self, name: ToolName) -> Tool[Any, Any] | None:
        return self._index.get(name)


class ToolRegistry:
    """
    Owner коллекции `ToolSource`'ов
    """

    def __init__(
        self,
        sources: Iterable[ToolSource],
    ) -> None:
        self._sources: dict[ToolSourceId, ToolSource] = {}
        for src in sources:
            sid = src.id()

            # колизия по id источника — это ошибка
            # т.к. мы не сможем понять, к какому source отнести tool_id
            if sid in self._sources:
                raise ToolSourceCollisionError(sid)

            self._sources[sid] = src

    @property
    def sources(self) -> Mapping[ToolSourceId, ToolSource]:
        return self._sources

    def catalog(self) -> ToolCatalog:
        """Read-only view для LLM: только `definitions()`."""
        return ToolCatalog(self._sources)

    def executor(self) -> ToolExecutor:
        """Execute-only view для middleware: только `execute(ctx, req)`."""
        return ToolExecutor(self._sources)

    def close(self) -> None:
        """
        для поддержки graceful shutdown: закрыть все source'ы и container
        Ошибки одного не блокируют остальных
        """
        errors: list[Exception] = []
        for src in self._sources.values():
            try:
                src.close()
            except Exception as e:
                errors.append(e)

        if errors:
            raise ExceptionGroup("errors during ToolRegistry.close", errors)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ToolCatalog:
    """
    Read-only view над набором source'ов: отдаёт definitions для LLM.

    Lifecycle принадлежит `ToolRegistry`. Catalog хранит reference на тот же
    словарь source'ов, без owning-семантики.
    """

    def __init__(self, sources: Mapping[ToolSourceId, ToolSource]) -> None:
        self._sources = sources

    def definitions(self) -> Iterator[ToolSchema]:
        """Описания tool'ов для LLM: упакованные `ToolSchema` (name + schema)."""
        for src in self._sources.values():
            for tool in src.tools():
                yield tool.definition()


class ToolExecutor(Executor[ToolContext, ToolCall, ToolResult]):
    """
    Execute над набором ToolSource
    парсит ToolId и диспетчеризует его выполнение в нужный source
    """

    def __init__(self, sources: Mapping[ToolSourceId, ToolSource]) -> None:
        self._sources = sources

    def execute(self, ctx: ToolContext, req: ToolCall) -> ToolResult:
        try:
            source_id, name = parse_tool_id(req.tool_id)
        except ValueError as e:
            raise self._unknown_tool(req.tool_id) from e

        source = self._sources.get(source_id)
        if source is None:
            raise self._unknown_tool(req.tool_id)

        tool = source.find(name)
        if tool is None:
            raise self._unknown_tool(req.tool_id)

        try:
            return tool.invoke(ctx, req.arguments)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(
                tool.tool_id(),
                f"{type(e).__name__}: {e}",
            ) from e

    def _unknown_tool(self, tool_id: ToolId) -> ToolExecutionError:
        available = sorted(
            tool.tool_id() for src in self._sources.values() for tool in src.tools()
        )

        if not available:
            msg = f"tool {tool_id!r} not found; no tools are registered"
        else:
            msg = (
                f"tool {tool_id!r} not found. "
                f"available: {', '.join(repr(a) for a in available)}"
            )

        return ToolExecutionError(tool_id, msg)
