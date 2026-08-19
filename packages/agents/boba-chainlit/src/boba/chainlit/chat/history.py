"""История треда: чтение из checkpointer, replay в ленту, запись и откат.

Лента собирается из сообщений графа тем же ChatView, что и live: id шагов
детерминированы, поэтому история и стрим дают одинаковую раскладку. Исходы
хода пишет GraphTurnHistory; правка вопроса приходит обычным on_message,
и ThreadRewind усекает историю и вложения до состояния «сразу после вопроса».
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.chainlit.agent.chat_model import ResponseField
from boba.chainlit.agent.flow import PrefetchCall
from boba.chainlit.chat.turn import TurnMark, TurnRecord
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepRole,
    StepText,
    TurnDraft,
)
from chainlit.data.base import BaseDataLayer
from chainlit.step import StepDict

__all__ = [
    "CheckpointMessages",
    "ConversationTranscript",
    "GraphTurnHistory",
    "PendingCall",
    "RewindPlan",
    "ThreadMessages",
    "ThreadRewind",
    "TranscriptFeed",
]

logger = logging.getLogger(__name__)


@dataclass
class PendingCall:
    """Вызов инструмента, ждущий своего ToolMessage при сборке ленты."""

    name: str
    args: Mapping[str, Any]

    @classmethod
    def of(cls, call: Mapping[str, Any]) -> PendingCall:
        """Разбирает tool_call langchain: имя и аргументы могут не приехать."""
        name = call.get("name")
        if not name:
            name = ""

        args = call.get("args")
        if not isinstance(args, Mapping):
            args = {}

        return cls(name=str(name), args=cast("Mapping[str, Any]", args))


class GraphTurnHistory:
    """Пишет записи хода в состояние графа: их читает и лента, и сам агент."""

    def __init__(self, graph: CompiledStateGraph, thread_id: str) -> None:
        self._graph = graph
        self._thread_id = thread_id

    async def remember(self, record: TurnRecord) -> None:
        await self._graph.aupdate_state(
            RunnableConfig(configurable={"thread_id": self._thread_id}),
            {"messages": [record.message()]},
        )
        logger.info("history record written: %s", record.mark.value)


class ThreadMessages(Protocol):
    """Источник сообщений треда, из которых собирается лента."""

    async def load(self, thread_id: str) -> list[BaseMessage]: ...


class TranscriptFeed:
    """Лента треда для слоя данных: история checkpointer'а разворачивается в шаги.

    Реализует контракт ThreadFeed, объявленный слоем данных: отрисовка знает про
    хранилище, а не наоборот.
    """

    def __init__(self, messages: ThreadMessages) -> None:
        self._messages = messages

    async def steps(self, thread_id: str, user_name: str | None) -> Sequence[StepDict]:
        messages = await self._messages.load(thread_id)
        if not messages:
            return []

        sink = RecordingSink()
        view = ChatView(thread_id, sink, user_name=user_name)
        await ConversationTranscript(messages, view).replay()
        return sink.steps


class CheckpointMessages:
    """Читает сообщения треда из langgraph-checkpointer'а."""

    def __init__(self, saver: BaseCheckpointSaver) -> None:
        self._saver = saver

    async def load(self, thread_id: str) -> list[BaseMessage]:
        config = RunnableConfig(configurable={"thread_id": thread_id})
        snapshot = await self._saver.aget_tuple(config)
        if snapshot is None:
            return []

        values = snapshot.checkpoint.get("channel_values") or {}
        messages = values.get("messages") or []
        return [m for m in messages if isinstance(m, BaseMessage)]


class ConversationTranscript:
    """Разворачивает список сообщений агента в шаги ленты."""

    def __init__(self, messages: Sequence[BaseMessage], view: ChatView) -> None:
        self._messages = messages
        self._view = view
        self._pending: dict[str, PendingCall] = {}
        self._turn = TurnDraft()
        self._stage_queries: list[str] = []

    async def replay(self) -> None:
        for index, message in enumerate(self._messages):
            key = message.id
            if not key:
                key = f"#{index}"

            match message:
                case HumanMessage():
                    await self._close_stage()
                    self._view.begin_turn(message.id)
                    self._pending.clear()
                    self._turn = TurnDraft(key=message.id)
                    await self._view.question(self._text(message), message.id)
                case ToolMessage():
                    await self._open_stage(message)
                    await self._tool(message, key)
                case AIMessage():
                    if not self._prepares(message):
                        await self._close_stage()
                    await self._assistant(message, key)
                case _:
                    continue

        await self._close_stage()

    @staticmethod
    def _prepares(message: AIMessage) -> bool:
        """Сообщение подготовки: его вызовы рисуются этапом, а не ходом."""
        for call in message.tool_calls:
            if PrefetchCall.marks(call.get("id")):
                return True

        return False

    async def _open_stage(self, message: ToolMessage) -> None:
        """Первый ответ подготовки открывает этап, остальные копят запросы."""
        if not PrefetchCall.marks(message.tool_call_id):
            return

        if self._view.stage_step is None:
            await self._view.begin_stage(StepText.PREFETCH.value)

        call = self._pending.get(message.tool_call_id)
        if call is None:
            return

        query = call.args.get("query")
        if not query:
            return

        text = str(query)
        if text in self._stage_queries:
            return

        self._stage_queries.append(text)

    async def _close_stage(self) -> None:
        """Закрывает этап подготовки; без открытого этапа закрывать нечего."""
        if self._view.stage_step is None:
            return

        await self._view.end_stage(self._stage_queries)
        self._stage_queries = []

    async def _assistant(self, message: AIMessage, key: str) -> None:
        if self._is_error(message):
            await self._view.error(self._text(message), self._answer_key(key))
            return

        if self._is_stopped(message):
            await self._stopped(message, key)
            return

        if reasoning := self._reasoning(message):
            await self._view.thinking(reasoning, key)

        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id:
                continue

            self._pending[call_id] = PendingCall.of(call)

        if text := self._text(message):
            await self._view.answer(text, self._answer_key(key))

    async def _tool(self, message: ToolMessage, key: str) -> None:
        call = self._pending.pop(message.tool_call_id, None)

        name = message.name
        if not name and call is not None:
            name = call.name
        if not name:
            name = StepText.TOOL.value

        args: Mapping[str, Any] | None = None
        if call is not None:
            args = call.args

        call_key = message.tool_call_id
        if not call_key:
            call_key = key

        step = await self._view.tool_started(name, args, call_key)

        if message.status == "error":
            await self._view.tool_failed(step, self._text(message))
            return

        artifact = message.artifact
        if artifact is None:
            artifact = self._text(message)
        await self._view.tool_finished(step, artifact, message.tool_call_id)

    def _answer_key(self, key: str) -> str:
        """Ключ очередного ответа хода; вне хода адресуемся самим сообщением."""
        turn_key = self._turn.next_answer_key()
        if turn_key is None:
            return key

        return turn_key

    async def _stopped(self, message: AIMessage, key: str) -> None:
        """Прерванный ход: пометка остановки уже вшита в текст записи."""
        if reasoning := self._reasoning(message):
            await self._view.thinking(reasoning, key)

        if text := self._text(message):
            await self._view.answer(text, self._answer_key(key))

    @staticmethod
    def _is_error(message: AIMessage) -> bool:
        extra = message.additional_kwargs
        if not extra:
            return False

        return bool(extra.get(TurnMark.ERROR.value))

    @staticmethod
    def _is_stopped(message: AIMessage) -> bool:
        extra = message.additional_kwargs
        if not extra:
            return False

        return bool(extra.get(TurnMark.STOPPED.value))

    @staticmethod
    def _reasoning(message: AIMessage) -> str:
        extra = message.additional_kwargs
        if not extra:
            return ""

        value = extra.get(ResponseField.REASONING_CONTENT.value)
        if not value:
            return ""

        return str(value)

    @staticmethod
    def _text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, Mapping) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            return "".join(parts)
        return str(content)


class RewindPlan:
    """Что убрать при откате: сообщения истории и вложения их шагов."""

    def __init__(
        self,
        remove_ids: Sequence[str],
        element_ids: Sequence[str],
    ) -> None:
        self.remove_ids = list(remove_ids)
        self.element_ids = list(element_ids)

    def __bool__(self) -> bool:
        return bool(self.remove_ids or self.element_ids)


class ThreadRewind:
    """Приводит историю треда к состоянию «сразу после этого вопроса»."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        data_layer: BaseDataLayer,
        thread_id: str,
    ) -> None:
        self._graph = graph
        self._data_layer = data_layer
        self._thread_id = thread_id

    @property
    def _config(self) -> RunnableConfig:
        return RunnableConfig(configurable={"thread_id": self._thread_id})

    async def messages(self) -> list[BaseMessage]:
        """Текущая история треда."""
        state = await self._graph.aget_state(self._config)
        return list(state.values.get("messages", []))

    async def is_edit(self, message_id: str) -> bool:
        """Вопрос с таким id уже в истории — значит пришла правка."""
        return any(
            isinstance(m, HumanMessage) and m.id == message_id
            for m in await self.messages()
        )

    @staticmethod
    def plan(
        messages: Sequence[BaseMessage],
        message_id: str,
        thread_id: str,
    ) -> RewindPlan:
        """Хвост после вопроса: id сообщений и id их вложений."""
        index = None
        for position, message in enumerate(messages):
            if not isinstance(message, HumanMessage):
                continue
            if message.id != message_id:
                continue
            index = position
            break
        if index is None:
            return RewindPlan([], [])

        tail = messages[index + 1 :]
        remove_ids = [m.id for m in tail if m.id]

        element_ids: list[str] = []
        for message in tail:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls or ():
                element_id = ChatView.derive_id(
                    thread_id, call.get("id"), StepRole.ELEMENT
                )
                if element_id:
                    element_ids.append(element_id)

        return RewindPlan(remove_ids, element_ids)

    @staticmethod
    def prefix(messages: Sequence[BaseMessage], message_id: str) -> list[BaseMessage]:
        """История до правленого вопроса; вопроса и хвоста в ней нет."""
        kept: list[BaseMessage] = []
        for message in messages:
            if isinstance(message, HumanMessage) and message.id == message_id:
                return kept
            kept.append(message)

        return []

    async def apply(self, message_id: str, content: str) -> RewindPlan:
        """Удалить хвост и его вложения, поставить вопросу новый текст.

        Канал переписывается целиком: точечный RemoveMessage падает, когда
        прерванный ход оставил pending writes — aget_state их показывает,
        но в канале чекпойнта их нет.
        """
        messages = await self.messages()
        rewind = self.plan(messages, message_id, self._thread_id)

        for element_id in rewind.element_ids:
            await self._data_layer.delete_element(element_id, self._thread_id)

        updates: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
        updates.extend(self.prefix(messages, message_id))
        updates.append(HumanMessage(content=content, id=message_id))
        await self._graph.aupdate_state(self._config, {"messages": updates})
        return rewind

    async def refresh_view(self) -> None:
        """Перерисовывает ленту треда из истории агента."""
        if thread := await self._data_layer.get_thread(self._thread_id):
            await cl.context.emitter.resume_thread(thread)
