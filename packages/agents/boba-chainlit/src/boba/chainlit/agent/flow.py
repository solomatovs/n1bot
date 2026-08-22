"""Граф хода по flow профиля: профиль владеет сборкой агента langgraph.

PlainGraphBuilder собирает обычный цикл модель-инструменты. PrefetchGraphBuilder
дополняет его подготовкой каждого хода: запрос пользователя превращается в
поисковые (моделью-переформулировщиком либо как есть), инструменты flow
вызываются сразу, их результаты ложатся в состояние обменом tool_calls —
основная модель отвечает уже с готовым контекстом.

Ошибки:
PrefetchError — переформулировка сорвалась либо слой инструментов нарушил
    контракт ответа; сорванный вызов поиска ход не роняет, его причина едет
    к модели конвертом tool_result.
"""

from __future__ import annotations

import asyncio
import logging
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

from boba.llm.chat import ResponseField
from boba.toolkit.calls import ToolIntent
from boba.toolkit.failure import FailureText
from boba.toolkit.result import ErrorResult, ToolArtifact
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "AgentGraphBuilder",
    "GraphSpec",
    "LlmRephraser",
    "PassthroughRephraser",
    "PlainGraphBuilder",
    "PrefetchCall",
    "PrefetchError",
    "PrefetchGraphBuilder",
    "PrefetchMiddleware",
    "PrefetchStage",
    "PrefetchStamp",
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


class PrefetchStamp:
    """Длительность подготовки в сообщении её вызовов.

    Этап ленты собственного сообщения не имеет: живой показ знает время по
    часам хода, а сборка истории — только по сообщениям. Пометка на AIMessage
    подготовки и даёт обеим лентам одну подпись.
    """

    KEY: ClassVar[str] = "prefetch_elapsed_ms"

    @classmethod
    def mark(cls, elapsed_ms: int) -> dict[str, Any]:
        return {cls.KEY: elapsed_ms}

    @classmethod
    def of(cls, message: AIMessage) -> int:
        """Длительность подготовки; 0 — сообщение её не несёт."""
        value = message.additional_kwargs.get(cls.KEY)
        if not isinstance(value, int):
            return 0

        return value


class Rephraser(Protocol):
    """Порт переформулировки запроса пользователя в поисковые."""

    @abstractmethod
    async def rephrase(self, query: str) -> Sequence[str]: ...


class PrefetchStage(Protocol):
    """Порт показа этапа подготовки: лента о самой подготовке ничего не знает."""

    @abstractmethod
    async def begin(self) -> None: ...

    @abstractmethod
    async def searching(self, queries: Sequence[str]) -> None: ...

    @abstractmethod
    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None: ...


class PassthroughRephraser(Rephraser):
    """Поиск идёт по исходному запросу: переформулировщик профилю не задан."""

    async def rephrase(self, query: str) -> Sequence[str]:
        return [query]


class LlmRephraser(Rephraser):
    """Переформулировка маленькой моделью вызовом инструмента.

    Схему ответа модель заполняет через function calling: response_format
    роутеры отклоняют, а разбирать свободный текст нечем — формат ответа
    задаёт модель, а не мы.
    """

    METHOD: ClassVar[str] = "function_calling"

    def __init__(self, chat: BaseChatModel, system_prompt: str, queries: int) -> None:
        self._chat = chat.with_structured_output(QueryRephrasings, method=self.METHOD)
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
        elapsed = Elapsed()

        await self._stage.begin()
        try:
            rephrased = await self._rephraser.rephrase(query)
            await self._stage.searching(rephrased)

            calls = self._calls(rephrased)
            results = await self._invoke(calls)
        except PrefetchError:
            raise
        except Exception as exc:
            raise PrefetchError(f"prefetch failed: {exc}") from exc
        finally:
            await self._stage.end(rephrased, elapsed.ms())

        return {"messages": [self._request(calls, elapsed.ms()), *results]}

    @staticmethod
    def _request(calls: Sequence[ToolCall], elapsed_ms: int) -> AIMessage:
        """Вызовы подготовки как сообщение ассистента.

        Пустое поле рассуждений обязательно: провайдер в режиме размышления
        отклоняет сообщение с вызовами, у которого его нет, а подготовка
        ничего не обдумывала.
        """
        marks: dict[str, Any] = {ResponseField.REASONING_CONTENT.value: ""}
        marks.update(PrefetchStamp.mark(elapsed_ms))

        return AIMessage(
            content="",
            tool_calls=list(calls),
            additional_kwargs=marks,
        )

    @staticmethod
    def _turn_start(messages: Sequence[BaseMessage]) -> bool:
        """Начало хода: последним в состоянии лежит вопрос пользователя."""
        if not messages:
            return False

        return isinstance(messages[-1], HumanMessage)

    def _calls(self, queries: Sequence[str]) -> list[ToolCall]:
        """ToolCall-конверты: каждая переформулировка в каждый инструмент flow.

        Подпись вызова заполняет подготовка, а не модель: шаг ленты называет
        запрос, с которым инструмент пошёл искать.
        """
        calls: list[ToolCall] = []
        for query in queries:
            for tool in self._tools:
                call = ToolCall(
                    name=tool.name,
                    args={"query": query, ToolIntent.NAME: query},
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

        outputs = await asyncio.gather(*pending, return_exceptions=True)

        results: list[ToolMessage] = []
        for call, output in zip(calls, outputs, strict=True):
            results.append(self._checked(call, output))

        return results

    @classmethod
    def _checked(cls, call: ToolCall, output: object) -> ToolMessage:
        """Результат поиска: отказ инструмента едет в контекст, а не роняет ход.

        Модель получает ошибку тем же конвертом tool_result, что и удачный
        ответ, и решает сама — переспросить, вызвать инструмент ещё раз или
        ответить без него; пользователь видит крест на шаге ленты. Так же
        обрабатывается сорванный вызов — негодные аргументы, падение тела:
        ход продолжается, а причину читает модель. Останавливает ход только
        нарушение контракта самого слоя инструментов.
        """
        if isinstance(output, BaseException):
            return cls._failed(call, output)

        if not isinstance(output, ToolMessage):
            got = type(output).__name__
            raise PrefetchError(f"prefetch tool returned {got} instead of ToolMessage")

        if output.status == "error":
            logger.warning("prefetch %s failed: %s", output.name, output.content)
            return output

        artifact = ToolArtifact.revive(output.artifact)
        if isinstance(artifact, ErrorResult):
            logger.warning("prefetch %s failed: %s", output.name, artifact.message)

        return output

    @staticmethod
    def _failed(call: ToolCall, error: BaseException) -> ToolMessage:
        """Сорванный вызов конвертом tool_result; отмена хода идёт наверх."""
        if not isinstance(error, Exception):
            raise error

        name = call["name"]
        logger.warning("prefetch %s failed: %s", name, FailureText.of(error))

        call_id = call["id"]
        if not call_id:
            msg = f"prefetch call {name!r} has no id"
            raise PrefetchError(msg)

        return ToolMessage(
            content=f"tool failed {name!r}: {FailureText.of(error)}",
            tool_call_id=call_id,
            name=name,
            status="error",
        )


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
