"""
ToolSource + ToolRegistry + ToolCatalog + ToolExecutor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol, Self, runtime_checkable

from boba.patterns import Executor
from boba.tools.domain.errors import ToolExecutionError
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
)
from boba.tools.domain.tool import (
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSchema,
)
from boba.tools.framework.errors import (
    ToolIdCollisionError,
    ToolNameCollisionError,
    ToolSourceCollisionError,
)

__all__ = [
    "StaticToolSource",
    "ToolCatalog",
    "ToolExecutor",
    "ToolRegistry",
    "ToolSource",
]


@runtime_checkable
class Closeable(Protocol):
    """Что угодно с `close()` — DI-контейнер, клиент, пул."""

    def close(self) -> None: ...


class ToolSource(ABC):
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

    @abstractmethod
    def close(self) -> None: ...


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
            if tool.source_id() != source_id:
                msg = (
                    f"tool {tool.tool_id()!r} attached to source "
                    f"{source_id!r} but claims source "
                    f"{tool.source_id()!r}"
                )
                raise ValueError(msg)
            name = tool.name()
            if name in self._index:
                raise ToolIdCollisionError(source_id, name)
            self._index[name] = tool

    def id(self) -> ToolSourceId:
        return self._id

    def tools(self) -> Iterable[Tool[Any, Any]]:
        return iter(self._index.values())

    def find(self, name: ToolName) -> Tool[Any, Any] | None:
        return self._index.get(name)

    def close(self) -> None:
        pass  # нет ресурсов, которые нужно освобождать


class ToolRegistry:
    """
    Owner коллекции `ToolSource`'ов
    """

    def __init__(
        self,
        sources: Iterable[ToolSource],
        *,
        container: Closeable | None = None,
    ) -> None:
        self._sources: dict[ToolSourceId, ToolSource] = {}
        self._container = container

        # Плоский индекс wire-имя → tool: по нему идёт маршрутизация LLM-вызова.
        # Source в имя не входит, поэтому уникальность имени проверяется
        # глобально, между всеми source'ами.
        self._tools: dict[ToolId, Tool[Any, Any]] = {}
        self._tool_source: dict[ToolId, ToolSourceId] = {}

        for src in sources:
            sid = src.id()

            # колизия по id источника — это ошибка
            if sid in self._sources:
                raise ToolSourceCollisionError(sid)

            self._sources[sid] = src

            for tool in src.tools():
                tid = tool.tool_id()
                if tid in self._tools:
                    raise ToolNameCollisionError(
                        ToolName(tid),
                        self._tool_source[tid],
                        sid,
                    )
                self._tools[tid] = tool
                self._tool_source[tid] = sid

    @property
    def sources(self) -> Mapping[ToolSourceId, ToolSource]:
        return self._sources

    def catalog(self) -> ToolCatalog:
        """Read-only view для LLM: только `definitions()`."""
        return ToolCatalog(self._tools)

    def executor(self) -> ToolExecutor:
        """Execute-only view для middleware: только `execute(ctx, req)`."""
        return ToolExecutor(self._tools)

    def close(self) -> None:
        """Graceful shutdown: закрыть все source'ы и DI-контейнер.

        Ошибки одного не блокируют остальных — собираются в `ExceptionGroup`.
        Контейнер закрывается последним: его teardown (APP-scope провайдеры)
        не должен опережать освобождение ресурсов source'ов.
        """
        errors: list[Exception] = []
        for src in self._sources.values():
            try:
                src.close()
            except Exception as e:
                errors.append(e)

        if self._container is not None:
            try:
                self._container.close()
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
    Read-only view над плоским индексом tool'ов: отдаёт definitions для LLM.

    Lifecycle принадлежит `ToolRegistry`. Catalog хранит reference на тот же
    словарь tool'ов, без owning-семантики.
    """

    def __init__(self, tools: Mapping[ToolId, Tool[Any, Any]]) -> None:
        self._tools = tools

    def definitions(self) -> Iterator[ToolSchema]:
        """Описания tool'ов для LLM: упакованные `ToolSchema` (name + schema)."""
        for tool in self._tools.values():
            yield tool.definition()


class ToolExecutor(Executor[ToolContext, ToolCall, ToolResult]):
    """
    Execute над плоским индексом tool'ов: маршрутизирует вызов напрямую
    по wire-имени (`ToolId`), без парсинга источника.
    """

    def __init__(self, tools: Mapping[ToolId, Tool[Any, Any]]) -> None:
        self._tools = tools

    def execute(self, ctx: ToolContext, req: ToolCall) -> ToolResult:
        tool = self._tools.get(req.tool_id)
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
        available = sorted(self._tools)

        if not available:
            msg = f"tool {tool_id!r} not found; no tools are registered"
        else:
            msg = (
                f"tool {tool_id!r} not found. "
                f"available: {', '.join(repr(a) for a in available)}"
            )

        return ToolExecutionError(tool_id, msg)
