"""Граф хода по flow профиля: профиль владеет сборкой агента langgraph.

PlainGraphBuilder собирает обычный цикл модель-инструменты. PrefetchGraphBuilder
дополняет его подготовкой каждого хода: запрос пользователя превращается в
поисковые (моделью-переформулировщиком либо как есть), инструменты flow
вызываются сразу, их результаты ложатся в состояние обменом tool_calls —
основная модель отвечает уже с готовым контекстом. Инструменты и в цикле, и
в подготовке исполняет сервер инструментов за портом ToolServer: чат берёт у
него инструменты для модели, ServerCallMiddleware отдаёт ему вызовы ответа
модели, подготовка — свои.

Ошибки:
PrefetchError — слой инструментов нарушил контракт ответа; сорванный вызов
    поиска ход не роняет, его причина едет к модели конвертом tool_result,
    а сорванная переформулировка откатывается на исходный запрос.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolCall,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import override

from boba.chainlit.agent.bridge import ResponseField
from boba.llm.chat import LlmError, ToolSpec
from boba.llm.schema import SchemaReply
from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.result import FailureResult
from boba.toolkit.timing import Elapsed
from boba.toolrun.stream_calls import ToolServer

logger = logging.getLogger(__name__)

__all__ = [
    "AgentGraphBuilder",
    "GraphSpec",
    "LlmRephraser",
    "PassthroughRephraser",
    "PlainGraphBuilder",
    "PrefetchError",
    "PrefetchGraphBuilder",
    "PrefetchMiddleware",
    "PrefetchStage",
    "PrefetchStamp",
    "Rephraser",
    "Rephrasings",
    "RephrasingsParser",
    "ServerCallMiddleware",
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
    """Разбор объекта ответа переформулировщика: по схеме, иначе любые строки.

    Схему навязывает бэкенд, но модель вправе ответить объектом другой формы
    там, где ответ по существу верен: строковые значения и списки строк любого
    объекта тоже становятся запросами.
    """

    MAX_LENGTH: ClassVar[int] = 300

    def parse(self, reply: Mapping[str, Any]) -> Sequence[str]:
        by_schema = self._of_schema(reply)
        if by_schema:
            return by_schema

        return self._of_mapping(reply)

    @staticmethod
    def _of_schema(reply: Mapping[str, Any]) -> Sequence[str]:
        try:
            answer = Rephrasings.model_validate(reply)
        except ValidationError:
            return ()

        return answer.queries()

    def _of_mapping(self, reply: Mapping[str, Any]) -> Sequence[str]:
        """Любой объект: годятся строковые значения и списки строк."""
        found: list[str] = []
        for value in reply.values():
            self._collect(value, found)

        return found

    def _collect(self, value: object, found: list[str]) -> None:
        if isinstance(value, str):
            self._append(value, found)
            return

        if not isinstance(value, list):
            return

        for item in value:
            if not isinstance(item, str):
                continue

            self._append(item, found)

    def _append(self, value: str, found: list[str]) -> None:
        text = value.strip()
        if not text:
            return

        if len(text) > self.MAX_LENGTH:
            return

        if text in found:
            return

        found.append(text)


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
    """Переформулировка отдельной моделью: ответ по схеме через SchemaReply.

    Сорванная переформулировка ход не роняет: в инструменты уходит исходный
    запрос, а причина остаётся в журнале. Поиск по одному запросу хуже поиска
    по трём, но лучше отказа отвечать.
    """

    SCHEMA: ClassVar[ToolSpec] = ToolSpec(
        name=Rephrasings.__name__,
        description="Search variants of the user request.",
        parameters=Rephrasings.model_json_schema(),
    )

    def __init__(self, reply: SchemaReply, system_prompt: str) -> None:
        self._reply = reply
        self._system_prompt = system_prompt
        self._parser = RephrasingsParser()

    async def rephrase(self, query: str) -> Sequence[str]:
        try:
            answer = await self._reply.ask(self._system_prompt, query, self.SCHEMA)
        except LlmError as exc:
            logger.warning(
                "rephraser failed for query %r, searching as is: %s", query[:200], exc
            )
            return [query]

        rephrased = self._parser.parse(answer)
        if not rephrased:
            logger.warning(
                "rephraser returned nothing usable for query %r: %r",
                query[:200],
                dict(answer),
            )
            return [query]

        return rephrased


class PrefetchMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Подготовка контекста хода: поисковые запросы плюс вызовы инструментов.

    Срабатывает на каждый вопрос пользователя — в начале хода, когда последнее
    сообщение состояния пришло от него. Продолжения цикла, где модель уже
    ответила или сама зовёт инструменты, идут обычным графом. Вызовы
    подготовки исполняет сервер инструментов: каждая переформулировка в
    каждый инструмент — свой вызов, все идут одновременно.
    """

    def __init__(
        self,
        rephraser: Rephraser,
        tools: Sequence[str],
        stage: PrefetchStage,
        service: ToolServer,
    ) -> None:
        super().__init__()
        self._rephraser = rephraser
        self._tools = list(tools)
        self._stage = stage
        self._service = service

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
            msg = f"prefetch for query {query[:200]!r} failed: {exc}"
            raise PrefetchError(msg) from exc
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
                    name=tool,
                    args={"query": query, ToolIntent.NAME: query},
                    id=CallIdPrefix.PREFETCH.new_id(),
                    type="tool_call",
                )
                calls.append(call)

        return calls

    async def _invoke(self, calls: Sequence[ToolCall]) -> list[ToolMessage]:
        """Вызовы подготовки идут серверу одновременно; обрыв любого гасит
        остальные и уходит наверх."""
        pending: list[asyncio.Task[ToolMessage]] = []
        for call in calls:
            pending.append(asyncio.create_task(self._service.call(call)))

        try:
            replies = await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                task.cancel()

            raise

        results: list[ToolMessage] = []
        for reply in replies:
            results.append(self._checked(reply))

        return results

    @staticmethod
    def _checked(output: ToolMessage) -> ToolMessage:
        """Результат поиска: отказ инструмента едет в контекст, а не роняет ход.

        Модель получает ошибку тем же конвертом tool_result, что и удачный
        ответ, и решает сама — переспросить, вызвать инструмент ещё раз или
        ответить без него; пользователь видит крест на шаге ленты. Так же
        приходит сорванный вызов — негодные аргументы, падение тела:
        сервер упаковал его причину в результат-ошибку со статусом error.
        Отмена хода и нарушение контракта слоя инструментов идут наверх
        исключением.
        """
        if output.status == "error":
            logger.warning("prefetch %s failed: %s", output.name, output.content)
            return output

        if isinstance(output.artifact, FailureResult):
            logger.warning(
                "prefetch %s failed: %s", output.name, output.artifact.log_view()
            )

        return output


class ResponseCalls:
    """Вызовы одного ответа модели уходят серверу инструментов одним пакетом.

    ToolNode раздаёт вызовы ответа по одному и одновременно. Серверу они
    нужны вместе: потоковые вызовы одного ответа связаны именами каналов и
    идут одним запуском. Первый пришедший вызов ответа отправляет весь
    пакет, остальные берут из того же пакета своё ожидание; каждый вызов
    отвечает, как только готов сам, не дожидаясь соседей.
    """

    def __init__(self, service: ToolServer) -> None:
        self._service = service
        self._batches: dict[
            str, asyncio.Future[Sequence[asyncio.Future[ToolMessage]]]
        ] = {}
        self._waiting: dict[str, set[str]] = {}

    async def reply(self, request: ToolCallRequest) -> ToolMessage:
        calls = self._response_calls(request)
        call_id = str(request.tool_call["id"])
        batch_id = str(calls[0]["id"])

        batch = self._batches.get(batch_id)
        if batch is None:
            batch = asyncio.ensure_future(self._service.submit(calls))
            self._batches[batch_id] = batch
            self._waiting[batch_id] = self._ids_of(calls)

        pending = await batch
        own = self._own(calls, pending, call_id)

        try:
            return await own
        except asyncio.CancelledError:
            own.cancel()
            raise
        finally:
            self._taken(batch_id, call_id)

    def _taken(self, batch_id: str, call_id: str) -> None:
        """Вызов забрал свой итог; пакет забывается, когда забрали все."""
        waiting = self._waiting.get(batch_id)
        if waiting is None:
            return

        waiting.discard(call_id)
        if waiting:
            return

        self._batches.pop(batch_id, None)
        self._waiting.pop(batch_id, None)

    @staticmethod
    def _own(
        calls: Sequence[ToolCall],
        pending: Sequence[asyncio.Future[ToolMessage]],
        call_id: str,
    ) -> asyncio.Future[ToolMessage]:
        for call, future in zip(calls, pending, strict=True):
            if str(call["id"]) == call_id:
                return future

        msg = f"tool server accepted no call with id {call_id!r}"
        raise RuntimeError(msg)

    @staticmethod
    def _ids_of(calls: Sequence[ToolCall]) -> set[str]:
        ids: set[str] = set()
        for call in calls:
            ids.add(str(call["id"]))

        return ids

    @staticmethod
    def _response_calls(request: ToolCallRequest) -> Sequence[ToolCall]:
        """Все вызовы ответа модели, которому принадлежит вызов request."""
        own = request.tool_call
        for message in reversed(request.state["messages"]):
            if not isinstance(message, AIMessage):
                continue

            for call in message.tool_calls:
                if call["id"] == own["id"]:
                    return message.tool_calls

        return [own]


class ServerCallMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Вызовы инструментов исполняет сервер инструментов, а не ToolNode.

    ToolNode раздаёт вызовы ответа по одному; middleware собирает их в пакет
    ответа (ResponseCalls) и отдаёт порту ToolServer. Какой сервер стоит за
    инструментом, граф не знает. Вызов с выдуманным именем тоже уходит в
    порт: он отвечает, какие инструменты есть. Граф хода асинхронный;
    синхронный путь инструменты сервера не исполняет.
    """

    def __init__(self, service: ToolServer) -> None:
        super().__init__()
        self._calls = ResponseCalls(service)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        name = request.tool_call["name"]
        msg = (
            f"tool {name!r} is executed by a tool server, which is called "
            "in the async agent graph only"
        )
        raise RuntimeError(msg)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return await self._calls.reply(request)


@dataclass(frozen=True)
class GraphSpec:
    """Общие части графа хода: их собирает инфраструктура, билдер — компонует."""

    chat: BaseChatModel
    service: ToolServer
    """Порт инструментов: его инструменты получает модель, ему уходят вызовы."""
    system_prompt: str
    checkpointer: BaseCheckpointSaver
    history: AgentMiddleware[Any, Any, Any]
    """Представление истории для модели: обрезка и чистка чужих tool-вызовов."""


class GraphTools:
    """Инструменты графа хода и middleware их вызовов по порту инструментов."""

    def __init__(self, spec: GraphSpec) -> None:
        self.tools = list(spec.service.tools())
        self.middleware = ServerCallMiddleware(spec.service)


class AgentGraphBuilder(ABC):
    """Сборка графа хода; вид графа выбирает flow профиля."""

    @abstractmethod
    def build(self, spec: GraphSpec) -> CompiledStateGraph: ...


class PlainGraphBuilder(AgentGraphBuilder):
    """Обычный цикл модель-инструменты."""

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        graph = GraphTools(spec)

        return create_agent(
            model=spec.chat,
            tools=graph.tools,
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpointer,
            middleware=[spec.history, graph.middleware],
        )


class PrefetchGraphBuilder(AgentGraphBuilder):
    """Цикл с подготовкой контекста перед каждым обращением к модели."""

    def __init__(
        self,
        rephraser: Rephraser,
        tools: Sequence[str],
        stage: PrefetchStage,
    ) -> None:
        """tools — имена инструментов сервера, которые зовёт подготовка."""
        self._rephraser = rephraser
        self._tools = list(tools)
        self._stage = stage

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        graph = GraphTools(spec)
        prefetch = PrefetchMiddleware(
            self._rephraser, self._tools, self._stage, spec.service
        )

        return create_agent(
            model=spec.chat,
            tools=graph.tools,
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpointer,
            middleware=[prefetch, spec.history, graph.middleware],
        )
