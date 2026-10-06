"""Граф хода по flow профиля: профиль владеет сборкой агента langgraph.

PlainGraphBuilder собирает обычный цикл модель-инструменты. PrefetchGraphBuilder
дополняет его подготовкой каждого хода: запрос пользователя превращается в
поисковые (моделью-переформулировщиком либо как есть), инструменты flow
вызываются сразу, их результаты ложатся в состояние обменом tool_calls —
основная модель отвечает уже с готовым контекстом. Инструменты и в цикле, и
в подготовке исполняет сервер инструментов за портом ToolServer. Порт
нейтрален, граф говорит типами langchain; между ними стоит LangchainPort:
граф берёт у него заглушки инструментов для модели, ServerCallMiddleware
отдаёт ему вызовы ответа модели. Подготовка зовёт порт напрямую узлами
DagNode. HistoryView решает, какую часть истории треда видит модель.

Ошибки:
PrefetchError — слой инструментов нарушил контракт ответа; сорванный вызов
    поиска ход не роняет, его причина едет к модели конвертом tool_result,
    а сорванная переформулировка откатывается на исходный запрос.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables.config import ensure_config
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import override

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.agent.events import TurnEvents
from boba.chainlit.agent.history import CheckpointMessages
from boba.chainlit.agent.tools import LangchainPort
from boba.chainlit.chat.dialog import PrefetchStage
from boba.llm.chat import LlmError, ToolSpec
from boba.llm.schema import SchemaReply
from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.dag import DagNode, NodeOutcome, ToolServer
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "AgentGraphBuilder",
    "GraphSpec",
    "HistoryView",
    "LlmRephraser",
    "PassthroughRephraser",
    "PlainGraphBuilder",
    "PrefetchError",
    "PrefetchGraphBuilder",
    "PrefetchMiddleware",
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


class Rephraser(Protocol):
    """Порт переформулировки запроса пользователя в поисковые."""

    @abstractmethod
    async def rephrase(self, query: str) -> Sequence[str]: ...


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
    подготовки исполняет порт инструментов узлами DagNode: каждая
    переформулировка в каждый инструмент — свой вызов, все идут одновременно.
    В состояние графа вызовы и их итоги ложатся сообщениями langchain.
    Этап подготовки показывает порт PrefetchStage хода: его несёт
    колбэк-обработчик прогона (TurnEvents); прогон без него идёт без показа.
    """

    def __init__(
        self,
        rephraser: Rephraser,
        tools: Sequence[str],
        service: ToolServer,
    ) -> None:
        super().__init__()
        self._rephraser = rephraser
        self._tools = list(tools)
        self._service = service
        self._messages = LangchainMessages()

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
        stage = self._stage()

        if stage is not None:
            await stage.begin()

        try:
            rephrased = await self._rephraser.rephrase(query)
            if stage is not None:
                await stage.searching(rephrased)

            calls = self._calls(rephrased)
            outcomes = await self._invoke(calls)
        except PrefetchError:
            raise
        except Exception as exc:
            msg = f"prefetch for query {query[:200]!r} failed: {exc}"
            raise PrefetchError(msg) from exc
        finally:
            if stage is not None:
                await stage.end(rephrased, elapsed.ms())

        results: list[BaseMessage] = [
            self._messages.prefetch_request(calls, elapsed.ms())
        ]
        for outcome in outcomes:
            results.append(self._messages.tool_message(outcome))

        return {"messages": results}

    @staticmethod
    def _stage() -> PrefetchStage | None:
        """Порт этапа подготовки текущего прогона; None — ход идёт без
        слушателей чата (прогон графа напрямую).
        """
        config = ensure_config()

        callbacks = config.get("callbacks")
        handlers = getattr(callbacks, "handlers", None)
        if not handlers:
            return None

        for handler in handlers:
            if isinstance(handler, TurnEvents):
                return handler.stage

        return None

    @staticmethod
    def _turn_start(messages: Sequence[BaseMessage]) -> bool:
        """Начало хода: последним в состоянии лежит вопрос пользователя."""
        if not messages:
            return False

        return isinstance(messages[-1], HumanMessage)

    def _calls(self, queries: Sequence[str]) -> list[DagNode]:
        """Узлы вызовов: каждая переформулировка в каждый инструмент flow.

        Подпись вызова заполняет подготовка, а не модель: шаг ленты называет
        запрос, с которым инструмент пошёл искать.
        """
        calls: list[DagNode] = []
        for query in queries:
            for tool in self._tools:
                call = DagNode(
                    key=CallIdPrefix.PREFETCH.new_id(),
                    tool=tool,
                    args={"query": query, ToolIntent.NAME: query},
                )
                calls.append(call)

        return calls

    async def _invoke(self, calls: Sequence[DagNode]) -> list[NodeOutcome]:
        """Вызовы подготовки идут порту одновременно; обрыв любого гасит
        остальные и уходит наверх."""
        pending: list[asyncio.Task[NodeOutcome]] = []
        for call in calls:
            pending.append(asyncio.create_task(self._service.call(call)))

        try:
            replies = await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                task.cancel()

            raise

        for reply in replies:
            self._checked(reply)

        return replies

    @staticmethod
    def _checked(outcome: NodeOutcome) -> None:
        """Итог поиска: отказ инструмента едет в контекст, а не роняет ход.

        Модель получает ошибку тем же конвертом tool_result, что и удачный
        ответ, и решает сама — переспросить, вызвать инструмент ещё раз или
        ответить без него; пользователь видит крест на шаге ленты. Так же
        приходит сорванный вызов — негодные аргументы, падение тела:
        порт упаковал его причину в итог-отказ. Отмена хода и нарушение
        контракта слоя инструментов идут наверх исключением.
        """
        if outcome.ok():
            return

        logger.warning("prefetch %s failed: %s", outcome.tool, outcome.error_text())


class ServerCallMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Вызовы инструментов исполняет порт инструментов, а не ToolNode.

    ToolNode раздаёт вызовы ответа по одному; middleware отдаёт каждый
    стыку графа с портом (LangchainPort) — тот собирает их в пакет ответа.
    Какой сервер стоит за инструментом, граф не знает. Вызов с выдуманным
    именем тоже уходит в порт: он отвечает, какие инструменты есть. Граф
    хода асинхронный; синхронный путь инструменты порта не исполняет.
    """

    def __init__(self, port: LangchainPort) -> None:
        super().__init__()
        self._port = port

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
        return await self._port.reply(request)


class HistoryView(AgentMiddleware[AgentState[Any], Any, Any]):
    """Что видит модель из истории треда: middleware вызова модели.

    Создаётся сборкой чата на сессию и встаёт в граф хода (GraphSpec.history).
    Перед каждым обращением к модели оставляет от прошлых ходов последние
    вопросы и ответы без обмена с инструментами, а текущий ход отдаёт
    целиком, убирая вызовы инструментов, которых у сессии нет. Пути
    вложений вопроса дописывает в его текст: в истории они лежат отдельно,
    чтобы не попадать в ленту.
    """

    ATTACHMENTS_NOTE: ClassVar[str] = (
        "Прикреплённые файлы, доступны инструменту bash по этим путям:"
    )

    def __init__(
        self, allowed_tools: frozenset[str] | None, history_messages: int
    ) -> None:
        """allowed_tools — инструменты сессии; None — чужих вызовов нет.
        history_messages — сколько сообщений прошлых ходов видит модель."""
        super().__init__()
        self._allowed_tools = allowed_tools
        self._history_messages = history_messages
        self._messages = LangchainMessages()

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        view = self.of(request.state["messages"])

        return await handler(request.override(messages=view))

    def of(self, messages: Sequence[AnyMessage]) -> list[AnyMessage]:
        """Сообщения, которые уйдут модели, из полной истории треда."""
        start = self._last_question(messages)
        replies = list(self._replies(messages[:start]))

        view = replies[-self._history_messages :]
        view.extend(self._own_calls(messages[start:]))

        shown: list[AnyMessage] = []
        for message in view:
            shown.append(self._with_attachments(message))

        return shown

    @staticmethod
    def _last_question(messages: Sequence[AnyMessage]) -> int:
        """Позиция последнего вопроса пользователя: с него начинается
        текущий ход; без вопроса текущий ход — вся история."""
        for index in range(len(messages) - 1, -1, -1):
            if isinstance(messages[index], HumanMessage):
                return index

        return 0

    @staticmethod
    def _replies(messages: Sequence[AnyMessage]) -> Iterator[AnyMessage]:
        """Прошлые ходы без обмена с инструментами."""
        for message in messages:
            if isinstance(message, ToolMessage):
                continue

            if isinstance(message, AIMessage) and message.tool_calls:
                continue

            yield message

    def _own_calls(self, messages: Sequence[AnyMessage]) -> Iterator[AnyMessage]:
        """Текущий ход без вызовов инструментов, которых у сессии нет:
        сообщение с чужим вызовом уходит целиком вместе с ответами на него."""
        allowed = self._allowed_tools
        if allowed is None:
            yield from messages
            return

        dropped: set[str] = set()
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id in dropped:
                continue

            if not isinstance(message, AIMessage):
                yield message
                continue

            if not self._calls_foreign(message, allowed):
                yield message
                continue

            for call in message.tool_calls:
                if call_id := call["id"]:
                    dropped.add(call_id)

    @staticmethod
    def _calls_foreign(message: AIMessage, allowed: frozenset[str]) -> bool:
        for call in message.tool_calls:
            if call["name"] in allowed:
                continue

            return True

        return False

    def _with_attachments(self, message: AnyMessage) -> AnyMessage:
        """Дописывает пути вложений в текст вопроса: в ленте их быть не должно."""
        if not isinstance(message, HumanMessage):
            return message

        attachments = self._messages.user(message).attachments
        if not attachments:
            return message

        lines: list[str] = []
        for attachment in attachments:
            lines.append(f"- {attachment.name}: {attachment.path}")

        listing = "\n".join(lines)
        content = f"{message.content}\n\n{self.ATTACHMENTS_NOTE}\n{listing}"

        return message.model_copy(update={"content": content})


@dataclass(frozen=True)
class GraphSpec:
    """Общие части графа хода: их собирает инфраструктура, билдер — компонует."""

    chat: BaseChatModel
    service: ToolServer
    """Порт инструментов: его инструменты получает модель, ему уходят вызовы."""
    system_prompt: str
    checkpoints: CheckpointMessages
    """История тредов: на её хранилище граф ведёт checkpoint'ы."""
    history: HistoryView
    """Представление истории для модели: обрезка и чистка чужих tool-вызовов."""


class AgentGraphBuilder(ABC):
    """Сборка графа хода; вид графа выбирает flow профиля."""

    @abstractmethod
    def build(self, spec: GraphSpec) -> CompiledStateGraph: ...


class PlainGraphBuilder(AgentGraphBuilder):
    """Обычный цикл модель-инструменты."""

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        port = LangchainPort(spec.service)

        return create_agent(
            model=spec.chat,
            tools=port.tools(),
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpoints.saver,
            middleware=[spec.history, ServerCallMiddleware(port)],
        )


class PrefetchGraphBuilder(AgentGraphBuilder):
    """Цикл с подготовкой контекста перед каждым обращением к модели.

    Инструменты подготовки зовутся по именам через порт сессии, поэтому
    годятся и свои, и инструменты MCP-серверов профиля; имя, которого у
    порта нет, — отказ сборки графа.
    """

    def __init__(self, rephraser: Rephraser, tools: Sequence[str]) -> None:
        """tools — имена инструментов порта, которые зовёт подготовка."""
        self._rephraser = rephraser
        self._tools = list(tools)

    @override
    def build(self, spec: GraphSpec) -> CompiledStateGraph:
        self._check_tools(spec.service)
        port = LangchainPort(spec.service)
        prefetch = PrefetchMiddleware(self._rephraser, self._tools, spec.service)

        return create_agent(
            model=spec.chat,
            tools=port.tools(),
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpoints.saver,
            middleware=[prefetch, spec.history, ServerCallMiddleware(port)],
        )

    def _check_tools(self, service: ToolServer) -> None:
        known = service.names()

        for name in self._tools:
            if name in known:
                continue

            available = ", ".join(sorted(known))
            msg = (
                f"flow tool {name!r} is not available to the session; "
                f"available tools: {available}"
            )
            raise RuntimeError(msg)
