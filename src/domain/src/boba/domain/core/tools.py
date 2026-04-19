from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Self, TypeVar

from boba.domain.core.patterns import (
    Converter,
    Definition,
    Executor,
    ExecutorDispatcher,
    ExecutorRouteError,
    FoldFactory,
    Id,
    PrioritySource,
)

TArgs = TypeVar("TArgs")


class ToolId(Id[str]):
    """Уникальный идентификатор инструмента — сквозной ключ поиска и вызова."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class ToolSourceId(Id[str]):
    """Идентификатор источника инструментов (builtin, mcp:server_a, ...)."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class JsonType(Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class ParamSchema:
    """Описание одного параметра инструмента для LLM."""

    name: str
    type: JsonType
    description: str
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента."""

    params: list[ParamSchema]


@dataclass(frozen=True)
class ToolDefinition:
    """
    Описание инструмента для LLM: текст и схема параметров.

    ``id`` намеренно не хранится — он живёт на самом :class:`Tool`
    (:meth:`Tool.tool_id`) и считается источником правды. LLM-facing
    запись собирается на границе сервиса из пары
    ``(tool.tool_id(), tool.definition())``.
    """

    description: str
    input_schema: ToolInputSchema


@dataclass(frozen=True)
class ToolCall:
    """
    Запрос на вызов инструмента от LLM.

    ``tool_id`` — к какому инструменту;
    ``arguments`` — сырой dict, каким его сериализовала LLM.
    """

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Результат успешного выполнения инструмента, уходит в messages с ролью TOOL.

    Ошибки не представляются отдельным флагом — они бросаются как
    :class:`ToolExecutionError` и обрабатываются выше по стеку (middleware
    пишет ошибку в ``LLMMessage`` и эмитит ``ToolExecutionFailed``).
    """

    content: str


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента.

    Бросается из :meth:`ToolsService.execute` / :meth:`Tool.execute` вместо
    возврата ``ToolResult`` с флагом. Middleware ловит исключение, пишет
    сообщение ``role="tool"`` обратно в диалог (чтобы LLM на следующей
    итерации увидела ошибку и могла её починить) и эмитит событие
    ``ToolExecutionFailed`` для sink'ов. ``tool_call_id`` здесь не хранится —
    сервис его не знает; его добавляет вызывающий middleware в событие.
    """

    def __init__(self, tool_id: ToolId, message: str) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.message = message


class Tool(
    Executor[None, TArgs, ToolResult],
    Definition[ToolDefinition],
    Generic[TArgs],
):
    @abstractmethod
    def tool_id(self) -> ToolId: ...

    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...

    @abstractmethod
    def args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Конвертер сырых аргументов LLM в ``TArgs``."""
        ...


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
        self._items: dict[ToolId, Tool[Any]] = {
            tool.tool_id(): tool for tool in tools
        }

    def get(self, tool_id: ToolId) -> Tool[Any] | None:
        return self._items.get(tool_id)

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._items

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._items.values())

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех инструментов — для передачи в LLM."""
        return (tool.definition() for tool in self._items.values())


class ToolIdCollisionError(Exception):
    def __init__(
        self,
        tool_id: ToolId,
        existing_source: ToolSourceId,
        new_source: ToolSourceId,
    ) -> None:
        super().__init__(
            f"tool id {tool_id.to_wire()!r} already registered "
            f"by source {existing_source.to_wire()!r}; "
            f"rejected new source {new_source.to_wire()!r}"
        )
        self.tool_id = tool_id
        self.existing_source = existing_source
        self.new_source = new_source


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


class InvokableTool(Executor[None, ToolCall, ToolResult], Generic[TArgs]):
    """
    Адаптер ``Tool[TArgs]`` → ``Executor[None, ToolCall, ToolResult]``.

    Делает парсинг ``call.arguments`` через ``tool.args_converter()`` и
    вызывает ``tool.execute(None, args)``. Нужен, когда тулы кладутся как
    handler'ы в :class:`ExecutorDispatcher` (или любую другую композицию
    поверх ``Executor[None, ToolCall, ToolResult]``).
    """

    def __init__(self, tool: Tool[TArgs]) -> None:
        self._tool = tool

    def execute(self, ctx: None, call: ToolCall) -> ToolResult:
        args = self._tool.args_converter().convert(call.arguments)
        return self._tool.execute(ctx, args)


class ToolsService(Executor[None, ToolCall, ToolResult]):
    def __init__(
        self,
        factory: ToolFactory,
    ) -> None:
        self._factory = factory
        self._catalog: ToolCatalog = ToolCatalog([])
        self._dispatcher: ExecutorDispatcher[None, ToolCall, ToolResult] = (
            self._build_dispatcher(self._catalog)
        )

    @staticmethod
    def _build_dispatcher(
        catalog: ToolCatalog,
    ) -> ExecutorDispatcher[None, ToolCall, ToolResult]:
        routes: dict[ToolId, Executor[None, ToolCall, ToolResult]] = {
            tool.tool_id(): InvokableTool(tool) for tool in catalog.tools()
        }
        return ExecutorDispatcher(
            routes=routes,
            key_fn=lambda _ctx, call: call.tool_id,
        )

    def rebuild_catalog(self) -> None:
        """Пересобрать каталог и dispatcher под ним."""
        self._catalog = self._factory.build()
        self._dispatcher = self._build_dispatcher(self._catalog)

    def tools(self) -> Iterable[Tool[Any]]:
        """Все собранные инструменты — если нужны и id, и definition."""
        return self._catalog.tools()

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех собранных инструментов — для передачи в LLM."""
        return self._catalog.definitions()

    def execute(self, ctx: None, call: ToolCall) -> ToolResult:
        try:
            return self._dispatcher.execute(ctx, call)
        except ExecutorRouteError as e:
            raise self._unknown_tool(call.tool_id) from e
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
