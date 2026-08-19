"""Граф хода по flow профиля: профиль владеет сборкой агента langgraph.

PlainGraphBuilder собирает обычный цикл модель-инструменты. PrefetchGraphBuilder
дополняет его подготовкой каждого хода: запрос пользователя превращается в
поисковые (моделью-переформулировщиком либо как есть), инструменты flow
вызываются сразу, их результаты ложатся в состояние обменом tool_calls —
основная модель отвечает уже с готовым контекстом.

Ошибки:
PrefetchError — подготовка запросов или предварительный поиск сорвались,
    ход завершается сбоем.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field
from typing_extensions import override

from boba.chainlit.agent.chat_model import ResponseField
from boba.toolkit.result import ErrorResult, ToolArtifact

__all__ = [
    "AgentGraphBuilder",
    "GraphSpec",
    "LlmRephraser",
    "PlainGraphBuilder",
    "PrefetchCall",
    "PrefetchError",
    "PrefetchGraphBuilder",
    "PassthroughRephraser",
    "PrefetchMiddleware",
    "PrefetchStage",
    "QueryRephrasings",
    "Rephraser",
]


class PrefetchError(Exception):
    """Подготовка контекста хода сорвалась."""


class QueryRephrasings(BaseModel):
    """Ответ переформулировщика: поисковые варианты запроса пользователя."""

    queries: Annotated[
        Sequence[str],
        Field(min_length=1, description="Diverse search queries."),
    ]


class PrefetchCall:
    """Идентификатор вызова подготовки: по нему его узнаёт сборка ленты."""

    PREFIX: ClassVar[str] = "prefetch-"

    @classmethod
    def new_id(cls) -> str:
        return f"{cls.PREFIX}{uuid4().hex}"

    @classmethod
    def marks(cls, call_id: str | None) -> bool:
        """Вызов сделан подготовкой, а не моделью."""
        if not call_id:
            return False

        return call_id.startswith(cls.PREFIX)


class Rephraser(Protocol):
    """Порт переформулировки запроса пользователя в поисковые."""

    @abstractmethod
    async def rephrase(self, query: str) -> Sequence[str]: ...


class PrefetchStage(Protocol):
    """Порт показа этапа подготовки: лента о самой подготовке ничего не знает."""

    @abstractmethod
    async def begin(self) -> None: ...

    @abstractmethod
    async def end(self, queries: Sequence[str]) -> None: ...


class PassthroughRephraser:
    """Поиск идёт по исходному запросу: переформулировщик профилю не задан."""

    async def rephrase(self, query: str) -> Sequence[str]:
        return [query]


class LlmRephraser:
    """Переформулировка маленькой моделью вызовом инструмента.

    Схему ответа модель заполняет через function calling: response_format
    роутеры отклоняют, а разбирать свободный текст нечем — формат ответа
    задаёт модель, а не мы.
    """

    METHOD: ClassVar[str] = "function_calling"

    def __init__(self, chat: BaseChatModel, system_prompt: str, queries: int) -> None:
        self._chat = chat.with_structured_output(
            QueryRephrasings, method=self.METHOD
        )
        self._system_prompt = system_prompt
        self._queries = queries

    async def rephrase(self, query: str) -> Sequence[str]:
        order = f"Верни {self._queries} поисковых запроса"
        prompt = f"{self._system_prompt}\n\n{order}"
        messages = [SystemMessage(prompt), HumanMessage(query)]

        answer = await self._chat.ainvoke(messages)
        if not isinstance(answer, QueryRephrasings):
            got = type(answer).__name__
            raise PrefetchError(f"rephraser returned {got} instead of the schema")

        return list(answer.queries)[: self._queries]


class PrefetchMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Подготовка контекста хода: поисковые запросы плюс вызовы инструментов.

    Срабатывает на каждый вопрос пользователя — в начале хода, когда последнее
    сообщение состояния пришло от него. Продолжения цикла, где модель уже
    ответила или сама зовёт инструменты, идут обычным графом.
    """

    def __init__(
        self,
        rephraser: Rephraser,
        tools: Sequence[BaseTool],
        stage: PrefetchStage,
    ) -> None:
        super().__init__()
        self._rephraser = rephraser
        self._tools = list(tools)
        self._stage = stage

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        if not self._turn_start(messages):
            return None

        query = str(messages[-1].content)

        rephrased: Sequence[str] = ()

        await self._stage.begin()
        try:
            rephrased = await self._rephraser.rephrase(query)
            calls = self._calls(rephrased)
            results = await self._invoke(calls)
        except PrefetchError:
            raise
        except Exception as exc:
            raise PrefetchError(f"prefetch failed: {exc}") from exc
        finally:
            await self._stage.end(rephrased)

        return {"messages": [self._request(calls), *results]}

    @staticmethod
    def _request(calls: Sequence[ToolCall]) -> AIMessage:
        """Вызовы подготовки как сообщение ассистента.

        Пустое поле рассуждений обязательно: провайдер в режиме размышления
        отклоняет сообщение с вызовами, у которого его нет, а подготовка
        ничего не обдумывала.
        """
        return AIMessage(
            content="",
            tool_calls=list(calls),
            additional_kwargs={ResponseField.REASONING_CONTENT.value: ""},
        )

    @staticmethod
    def _turn_start(messages: Sequence[BaseMessage]) -> bool:
        """Начало хода: последним в состоянии лежит вопрос пользователя."""
        if not messages:
            return False

        return isinstance(messages[-1], HumanMessage)

    def _calls(self, queries: Sequence[str]) -> list[ToolCall]:
        """ToolCall-конверты: каждая переформулировка в каждый инструмент flow."""
        calls: list[ToolCall] = []
        for query in queries:
            for tool in self._tools:
                call = ToolCall(
                    name=tool.name,
                    args={"query": query},
                    id=PrefetchCall.new_id(),
                    type="tool_call",
                )
                calls.append(call)

        return calls

    async def _invoke(self, calls: Sequence[ToolCall]) -> list[ToolMessage]:
        by_name: dict[str, BaseTool] = {}
        for tool in self._tools:
            by_name[tool.name] = tool

        pending: list[Any] = []
        for call in calls:
            tool = by_name[call["name"]]
            pending.append(tool.ainvoke(call))

        outputs = await asyncio.gather(*pending)

        results: list[ToolMessage] = []
        for output in outputs:
            results.append(self._checked(output))

        return results

    @staticmethod
    def _checked(output: object) -> ToolMessage:
        """Результат поиска; отказ инструмента роняет ход, а не тонет в контексте."""
        if not isinstance(output, ToolMessage):
            got = type(output).__name__
            raise PrefetchError(f"prefetch tool returned {got} instead of ToolMessage")

        if output.status == "error":
            raise PrefetchError(f"prefetch search failed: {output.content}")

        artifact = ToolArtifact.revive(output.artifact)
        if isinstance(artifact, ErrorResult):
            raise PrefetchError(f"prefetch search failed: {artifact.message}")

        return output


@dataclass(frozen=True)
class GraphSpec:
    """Общие части графа хода: их собирает инфраструктура, билдер — компонует."""

    chat: BaseChatModel
    tools: Sequence[BaseTool]
    system_prompt: str
    checkpointer: BaseCheckpointSaver
    history: AgentMiddleware[Any, Any, Any]
    """Представление истории для модели: обрезка и чистка чужих tool-вызовов."""


class AgentGraphBuilder(ABC):
    """Сборка графа хода; вид графа выбирает flow профиля."""

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
            middleware=[spec.history],
        )


class PrefetchGraphBuilder(AgentGraphBuilder):
    """Цикл с подготовкой контекста перед каждым обращением к модели."""

    def __init__(
        self,
        rephraser: Rephraser,
        tools: Sequence[BaseTool],
        stage: PrefetchStage,
    ) -> None:
        self._prefetch = PrefetchMiddleware(rephraser, tools, stage)

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        return create_agent(
            model=spec.chat,
            tools=list(spec.tools),
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpointer,
            middleware=[self._prefetch, spec.history],
        )
