from dataclasses import dataclass
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, TypeVar, Generic, Iterator


class ToolId:
    """Идентификатор инструмента."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"ToolId({self._name!r})"


@dataclass(frozen=True)
class ToolParams:
    """Параметры инструмента"""

    pass


@dataclass(frozen=True)
class ToolResult:
    """Результат вызова инструмента, который придёт в messages с ролью TOOL."""

    content: str
    is_error: bool = False


class JsonType(Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class ParamSchema:
    """Описание одного параметра для LLM."""

    name: str
    type: JsonType
    description: str
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class ToolInputSchema:
    """Полная схема параметров инструмента."""

    params: list[ParamSchema]


@dataclass(frozen=True)
class ToolDefinition:
    """Определение инструмента для LLM: id, описание и схема параметров."""

    id: ToolId
    description: str
    input_schema: ToolInputSchema


TParams = TypeVar("TParams", bound=ToolParams)


class Tool(ABC, Generic[TParams]):
    """Инструмент, который может быть вызван LLM."""

    @abstractmethod
    def definition(self) -> ToolDefinition: ...

    @abstractmethod
    def params_type(self) -> type[TParams]: ...

    @abstractmethod
    async def execute(self, params: TParams) -> ToolResult: ...

    async def enter(self) -> None:
        pass

    async def close(self) -> None:
        pass


class ToolsService:
    """Сервис для управления инструментами: регистрация, получение определений, выполнение."""

    async def register(self, tool: Tool) -> None:
        """Зарегистрировать инструмент, вызвать tool.enter()."""
        ...

    async def unregister(self, id: ToolId) -> None:
        """Вызвать tool.close() и убрать инструмент."""
        ...

    def get_definitions(self) -> Iterator[ToolDefinition]:
        """Определения для параметра tools API."""
        ...

    async def execute(self, id: ToolId, raw_args: dict[str, Any]) -> ToolResult:
        """Найти tool, сконструировать params из raw JSON, выполнить."""
        ...

    async def close(self) -> None:
        """Вызвать close() у всех инструментов."""
        ...
