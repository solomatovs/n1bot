from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from boba.domain.core.patterns import Id


class ToolId(Id[str]):
    """Идентификатор инструмента."""


@dataclass(frozen=True)
class ToolParams:
    """Параметры инструмента"""



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
    def execute(self, params: TParams) -> ToolResult: ...


class ToolsService(ABC):
    """Сервис для управления инструментами:
    - регистрация
    - получение определений
    - выполнение."""

    @abstractmethod
    def register(self, tool: Tool) -> None:
        """Зарегистрировать инструмент, вызвать tool.enter()."""

    @abstractmethod
    def unregister(self, tool_id: ToolId) -> None:
        """Вызвать tool.close() и убрать инструмент."""

    @abstractmethod
    def get_definitions(self) -> Iterator[ToolDefinition]:
        """Определения для параметра tools API."""

    @abstractmethod
    def execute(
        self, tool_id: ToolId, raw_args: dict[str, Any]
    ) -> ToolResult:
        """Найти tool, сконструировать params из raw JSON, выполнить."""
