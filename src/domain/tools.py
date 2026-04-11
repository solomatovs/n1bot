"""Абстракции инструментов агента — Tool, ToolResult, ToolRegistry.

Streaming-модель: Tool.execute() — генератор, yield'ит pipeline-события
по мере выполнения, финальный yield — ToolResult с текстом для LLM.

Добавление нового инструмента:
    1. Создать класс-наследник Tool
    2. Добавить экземпляр в список при создании ToolRegistry
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Sequence

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
# ToolResult — финальный yield из Tool.execute()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolResult:
    """Текстовый результат инструмента — возвращается LLM.

    Должен быть последним yield из Tool.execute().
    Все промежуточные yield'ы — pipeline-события для UI.
    """
    content: str


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class Tool(ABC):
    """Базовый класс инструмента агента.

    Каждый инструмент — самодостаточная единица:
    определение (name, description, parameters) + исполнение (execute).

    execute() — генератор::

        def execute(self, **kwargs):
            yield SomeEvent(...)        # событие для UI (необязательно)
            yield ToolResult(content=...) # текст для LLM (обязательно, последний)
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
    def parameters(self) -> Dict[str, Any]:
        """JSON Schema параметров (OpenAI function parameters формат)."""

    @abstractmethod
    def execute(self, **kwargs: Any) -> Iterator[ToolResult | Any]:
        """Выполнить инструмент.

        Yields:
            Pipeline-события (для UI, необязательно).
            ToolResult — финальный текст для LLM (обязательно, последний yield).
        """

    @property
    def definition(self) -> Dict[str, Any]:
        """OpenAI-совместимое определение tool (авто-генерация из name/description/parameters)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Реестр инструментов — dispatch по имени.

    Создаётся один раз, передаётся в AgentLoopStage::

        registry = ToolRegistry([...tools...])
        for event in registry.execute("search_documents", query="..."):
            if isinstance(event, ToolResult):
                text = event.content
            else:
                handle_pipeline_event(event)
    """

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools: Dict[str, Tool] = {}
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

    def execute(self, name: str, **kwargs: Any) -> Iterator[ToolResult | Any]:
        """Выполнить инструмент по имени. Streaming — yield'ит события и ToolResult.

        Raises:
            ToolNotFoundError: инструмент не зарегистрирован.
            ToolExecutionError: ошибка во время выполнения.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(name)
        try:
            yield from tool.execute(**kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolExecutionError(name, exc) from exc
