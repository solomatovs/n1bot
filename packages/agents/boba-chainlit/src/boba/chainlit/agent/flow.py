"""Граф хода по flow профиля: профиль владеет сборкой агента langgraph.

PlainGraphBuilder собирает обычный цикл модель-инструменты. PrefetchGraphBuilder
дополняет его подготовкой каждого хода: запрос пользователя превращается в
поисковые (моделью-переформулировщиком либо как есть), инструменты flow
вызываются сразу, их результаты ложатся в состояние обменом tool_calls —
основная модель отвечает уже с готовым контекстом.

Ошибки:
PrefetchError — слой инструментов нарушил контракт ответа; сорванный вызов
    поиска ход не роняет, его причина едет к модели конвертом tool_result,
    а сорванная переформулировка откатывается на исходный запрос.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import override

from boba.chat.generation import GenerationError, SchemaSpec, StructuredGenerator
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
    "Rephraser",
    "Rephrasings",
    "RephrasingsParser",
]


class PrefetchError(Exception):
    """Подготовка контекста хода сорвалась."""


class Rephrasings(BaseModel):
    """Ответ переформулировщика: поисковые варианты запроса пользователя.

    Варианты названы полями, а не элементами списка: маленькая модель по
    безымянному массиву выдаёт один и тот же текст трижды, а по именам с
    описаниями заполняет каждое поле по существу.
    """

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={"additionalProperties": False},
    )

    keywords: str = Field(
        min_length=3,
        max_length=120,
        description="Key terms and product names only, no question words.",
    )

    expanded: str = Field(
        min_length=3,
        max_length=120,
        description="Full sentence with synonyms of the key terms.",
    )

    english: str = Field(
        min_length=3,
        max_length=120,
        description="The same request in English.",
    )

    def queries(self) -> Sequence[str]:
        """Непустые варианты без повторов; порядок объявления сохраняется."""
        found: list[str] = []
        for value in (self.keywords, self.expanded, self.english):
            text = value.strip()
            if not text:
                continue

            if text in found:
                continue

            found.append(text)

        return found


class RephrasingsParser:
    """Разбор ответа переформулировщика: схема, любой json, построчный текст.

    Схему держит грамматика локальной модели и объявление функции удалённой, но
    инференсы без поддержки того и другого отвечают текстом — там же, где ответ
    по существу верен. Каждая ступень разбирает свою форму, и до отката на
    исходный запрос дело доходит только на бессмысленном ответе.
    """

    NUMBERING: ClassVar[re.Pattern[str]] = re.compile(r"^\s*(?:[-*\d.)\s]+)")
    OBJECT_START: ClassVar[str] = "{"
    MAX_LENGTH: ClassVar[int] = 300

    @classmethod
    def parse(cls, raw: str) -> Sequence[str]:
        text = cls._json_text(raw)

        by_schema = cls._of_schema(text)
        if by_schema:
            return by_schema

        by_mapping = cls._of_mapping(text)
        if by_mapping:
            return by_mapping

        # оборванный по лимиту токенов json запросом быть не может
        if text.startswith(cls.OBJECT_START):
            return ()

        return cls._of_lines(text)

    @classmethod
    def _json_text(cls, raw: str) -> str:
        """Первый законченный json-объект в любой обёртке; без него — текст как есть."""
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char != cls.OBJECT_START:
                continue

            try:
                value, end = decoder.raw_decode(raw, index)
            except json.JSONDecodeError:
                continue

            if not isinstance(value, dict):
                continue

            if not value:
                continue

            return raw[index:end]

        return raw.strip()

    @staticmethod
    def _of_schema(text: str) -> Sequence[str]:
        try:
            answer = Rephrasings.model_validate_json(text)
        except ValidationError:
            return ()

        return answer.queries()

    @classmethod
    def _of_mapping(cls, text: str) -> Sequence[str]:
        """Любой json-объект: годятся строковые значения и списки строк."""
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return ()

        if not isinstance(loaded, dict):
            return ()

        found: list[str] = []
        for value in loaded.values():
            cls._collect(value, found)

        return found

    @classmethod
    def _collect(cls, value: object, found: list[str]) -> None:
        if isinstance(value, str):
            cls._append(value, found)
            return

        if not isinstance(value, list):
            return

        for item in value:
            if not isinstance(item, str):
                continue

            cls._append(item, found)

    @classmethod
    def _of_lines(cls, text: str) -> Sequence[str]:
        """Список строк: нумерация, маркеры и кавычки в запрос не идут."""
        found: list[str] = []
        for line in text.splitlines():
            stripped = cls.NUMBERING.sub("", line).strip().strip('"')
            cls._append(stripped, found)

        return found

    @classmethod
    def _append(cls, value: str, found: list[str]) -> None:
        text = value.strip()
        if not text:
            return

        if len(text) > cls.MAX_LENGTH:
            return

        if text in found:
            return

        found.append(text)


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
    """Переформулировка отдельной моделью; бэкенд задаёт профиль генерации.

    Сорванная переформулировка ход не роняет: в инструменты уходит исходный
    запрос, а причина остаётся в журнале. Поиск по одному запросу хуже поиска
    по трём, но лучше отказа отвечать.
    """

    SCHEMA: ClassVar[SchemaSpec] = SchemaSpec(
        name=Rephrasings.__name__,
        description="Search variants of the user request.",
        body=Rephrasings.model_json_schema(),
    )

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def rephrase(self, query: str) -> Sequence[str]:
        try:
            raw = await self._generator.generate(query, self.SCHEMA)
        except GenerationError as exc:
            logger.warning("rephraser failed, searching as is: %s", exc)
            return [query]

        rephrased = RephrasingsParser.parse(raw)
        if not rephrased:
            logger.warning("rephraser returned nothing usable: %r", raw[:200])
            return [query]

        return rephrased


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
