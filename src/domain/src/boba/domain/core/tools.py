"""Generic tool/dispatch framework.

Абстракция callable-инструментов с типизированными аргументами и
результатом; реестр источников, каталог, диспетчер вызовов. Модуль
**не завязан на LLM**: :class:`Tool` — это ``Executor[None, TArgs, ToolResult]``,
а вся LLM-специфика (tool_call_id, ``role="tool"`` сообщения, событие
``ToolExecutionFailed``) живёт в agent-слое, который этим фреймворком
пользуется.

В проекте потребитель ровно один — agent loop для LLM tool calling — но
сам модуль любой другой caller (CQRS-шина, RPC-диспетчер, …) мог бы
использовать без правок.
"""

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
    """Описание одного параметра инструмента."""

    name: str
    type: JsonType
    description: str
    required: bool = True
    default: Any = None
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента."""

    params: list[ParamSchema]


@dataclass(frozen=True)
class ToolDefinition:
    """
    Описание инструмента для потребителя: текст и схема параметров.

    ``id`` намеренно не хранится — он живёт на самом :class:`Tool`
    (:meth:`Tool.tool_id`) и считается источником правды. Запись, видимая
    потребителю, собирается на границе сервиса из пары
    ``(tool.tool_id(), tool.definition())``.
    """

    description: str
    input_schema: ToolInputSchema


@dataclass(frozen=True)
class ToolCall:
    """
    Запрос на вызов инструмента.

    ``tool_id`` — к какому инструменту;
    ``arguments`` — сырой dict, каким его сформировал caller (в случае
    LLM это то, что модель сериализовала в JSON).
    """

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Результат успешного выполнения инструмента.

    Ошибки не представляются отдельным флагом — они бросаются как
    :class:`ToolExecutionError` и обрабатываются caller-ом (в agent-слое
    middleware превращает их в tool-сообщение для LLM + наблюдательное
    событие).
    """

    content: str


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента.

    Бросается из :meth:`ToolsService.execute` / :meth:`Tool.execute` вместо
    возврата флагового результата. Обработка — на стороне caller-а: agent
    ловит, обогащает ``tool_call_id``-ом (который сервис не знает) и
    превращает в feedback-сообщение для LLM.
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
        """Конвертер сырых аргументов caller-а (напр. JSON от LLM) в ``TArgs``."""
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
        """Описания всех инструментов — для передачи потребителю."""
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


class ToolsService(Executor[None, ToolCall, ToolResult]):
    """Диспетчер tool-вызовов над :class:`ToolCatalog`.

    Маршрутизация встроена по ``call.tool_id``
    ищет :class:`Tool` в ToolCatalog и вызывает ``tool.execute``.

    Ошибка :class:`ToolExecutionError`:
    - Неизвестный tool
    - ошибка парсинга аргументов
    - произвольное исключение тула
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
