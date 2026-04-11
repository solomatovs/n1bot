"""Абстракции инструментов агента — Tool, ToolOutput, ToolRegistry.

Два generic-параметра::

    Tool[TEvent, TParams]
        TEvent  — тип промежуточных событий (DocPipelineEvent)
        TParams — dataclass параметров инструмента (SearchParams, ReadFileParams, ...)

Streaming-модель: Tool.execute() yield'ит ToolOutput[TEvent].
Единый возвращаемый тип, sealed-иерархия::

    ToolOutput[TEvent]
        ├── ToolEvent[TEvent]  — промежуточное событие для UI
        └── ToolResult         — финальный текст для LLM

Пример::

    @dataclass(frozen=True)
    class SearchParams:
        query: str
        top_k: int = 5

    class SearchDocumentsTool(Tool[DocPipelineEvent, SearchParams]):
        def execute(self, params: SearchParams) -> Iterator[ToolOutput[DocPipelineEvent]]:
            yield ToolEvent(SearchDone(hits=...))
            yield ToolResult(content="...")

Потребитель::

    for output in registry.execute("search_documents", query="..."):
        match output:
            case ToolEvent(event=e):   handle(e)
            case ToolResult(content=c): text = c
"""
from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Generic, Iterator, List, Sequence, TypeVar

from domain.errors import AppError


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

class ToolError(AppError):
    """Базовая ошибка инструмента."""


class ToolNotFoundError(ToolError):
    """Инструмент не найден в реестре."""

    def __init__(self, name: str) -> None:
        self.tool_name = name
        super().__init__(f"Unknown tool: {name}")


class ToolExecutionError(ToolError):
    """Ошибка выполнения инструмента."""

    def __init__(self, name: str, cause: Exception) -> None:
        self.tool_name = name
        self.cause = cause
        super().__init__(f"Tool {name} failed: {cause}")


# ---------------------------------------------------------------------------
# ToolOutput — единый тип выхода инструмента
# ---------------------------------------------------------------------------

TEvent = TypeVar("TEvent")
TParams = TypeVar("TParams")


class ToolOutput(Generic[TEvent]):
    """Единый тип yield из Tool.execute().

    Sealed-иерархия: ToolEvent | ToolResult.
    """
    __slots__ = ()


@dataclass(frozen=True)
class ToolEvent(ToolOutput[TEvent]):
    """Промежуточное событие для UI (обёртка над pipeline-событием)."""
    event: TEvent


@dataclass(frozen=True)
class ToolResult(ToolOutput):
    """Финальный текстовый результат для LLM.

    Должен быть последним yield из Tool.execute().
    """
    content: str


# ---------------------------------------------------------------------------
# Пустые параметры (для tools без аргументов)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmptyParams:
    """Параметры для инструментов без аргументов."""


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class Tool(ABC, Generic[TEvent, TParams]):
    """Базовый класс инструмента агента.

    Каждый инструмент — самодостаточная единица:
    определение + типизированные параметры + исполнение.

    TEvent  — тип промежуточных событий (DocPipelineEvent).
    TParams — frozen dataclass параметров (SearchParams, EmptyParams, ...).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя инструмента (snake_case)."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Описание для LLM — когда и зачем вызывать."""

    @property
    @abstractmethod
    def params_type(self) -> type[TParams]:
        """Класс параметров (frozen dataclass)."""

    @abstractmethod
    def execute(self, params: TParams) -> Iterator[ToolOutput[TEvent]]:
        """Выполнить инструмент с типизированными параметрами.

        Yields:
            ToolEvent[TEvent] — промежуточные события для UI.
            ToolResult — финальный текст для LLM (обязательно, последний yield).
        """

    @property
    def definition(self) -> Dict[str, Any]:
        """OpenAI-совместимое определение tool (авто-генерация из params_type)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _params_to_json_schema(self.params_type),
            },
        }


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry(Generic[TEvent]):
    """Реестр инструментов — dispatch по имени.

    Registry принимает сырые **kwargs из JSON (от LLM),
    конструирует типизированный params и передаёт в tool.execute().
    """

    def __init__(self, tools: Sequence[Tool[TEvent, Any]]) -> None:
        self._tools: Dict[str, Tool[TEvent, Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    @property
    def definitions(self) -> List[Dict[str, Any]]:
        """OpenAI-совместимые определения всех инструментов."""
        return [t.definition for t in self._tools.values()]

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def execute(self, name: str, raw_args: Dict[str, Any]) -> Iterator[ToolOutput[TEvent]]:
        """Выполнить инструмент по имени.

        raw_args — сырые аргументы из JSON (от LLM).
        Registry конструирует типизированный params автоматически.

        Raises:
            ToolNotFoundError: инструмент не зарегистрирован.
            ToolExecutionError: ошибка во время выполнения.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(name)
        try:
            params = _build_params(tool.params_type, raw_args)
            yield from tool.execute(params)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolExecutionError(name, exc) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_params(params_type: type, kwargs: Dict[str, Any]) -> Any:
    """Сконструировать типизированный params из сырого dict.

    Фильтрует неизвестные ключи — LLM может прислать лишние поля.
    """
    if params_type is EmptyParams or not kwargs:
        return params_type()
    known = {f.name for f in dataclasses.fields(params_type)}
    filtered = {k: v for k, v in kwargs.items() if k in known}
    return params_type(**filtered)


def _params_to_json_schema(params_type: type) -> Dict[str, Any]:
    """Сгенерировать JSON Schema из frozen dataclass параметров.

    Использует get_type_hints() для корректной работы
    с ``from __future__ import annotations``.
    """
    import typing

    if params_type is EmptyParams:
        return {"type": "object", "properties": {}}

    _TYPE_MAP: Dict[type, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }

    hints = typing.get_type_hints(params_type)
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for field in dataclasses.fields(params_type):
        resolved = hints.get(field.name, str)

        # Unwrap Optional: int | None → int
        base_type = resolved
        is_optional = False
        args = getattr(resolved, "__args__", None)
        if args and type(None) in args:
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                base_type = non_none[0]
                is_optional = True

        json_type = _TYPE_MAP.get(base_type, "string")
        prop: Dict[str, Any] = {"type": json_type}

        if field.metadata and "description" in field.metadata:
            prop["description"] = field.metadata["description"]

        if field.default is not dataclasses.MISSING:
            prop["default"] = field.default

        properties[field.name] = prop

        if field.default is dataclasses.MISSING and not is_optional:
            required.append(field.name)

    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
