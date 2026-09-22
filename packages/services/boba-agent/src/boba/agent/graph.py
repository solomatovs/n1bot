"""Граф хода: модель зовёт инструменты, пока ей это нужно, потом отвечает.

Сам цикл — `create_agent` из langchain: вызовы инструментов из ответа модели, их
параллельный запуск, результаты обратно в контекст, и так до ответа без вызовов.
Здесь только его сборка из частей, которые собирает вызывающий: модель, инструменты,
системный промпт, хранилище истории и промежуточные слои. Вид графа выбирает
построитель: чат подставляет свой, с подготовкой контекста перед моделью.

Ошибок своих нет: сборка графа чистая, всё, что может упасть, падает при запуске
хода у вызывающего.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import override

__all__ = ["AgentGraphBuilder", "GraphSpec", "PlainGraphBuilder"]


@dataclass(frozen=True)
class GraphSpec:
    """Части графа хода: их собирает вызывающий, построитель компонует."""

    chat: BaseChatModel
    tools: Sequence[BaseTool]
    system_prompt: str
    checkpointer: BaseCheckpointSaver
    """Хранилище истории по thread id: память процесса или postgres."""
    middleware: Sequence[AgentMiddleware[Any, Any, Any]] = ()
    """Слои вокруг обращения к модели: представление истории, пределы, подготовка."""


class AgentGraphBuilder(ABC):
    """Сборка графа хода; вид графа выбирает вызывающий."""

    @abstractmethod
    def build(self, spec: GraphSpec) -> CompiledStateGraph: ...


class PlainGraphBuilder(AgentGraphBuilder):
    """Обычный цикл модель-инструменты."""

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        return create_agent(
            model=spec.chat,
            tools=list(spec.tools),
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpointer,
            middleware=list(spec.middleware),
        )
