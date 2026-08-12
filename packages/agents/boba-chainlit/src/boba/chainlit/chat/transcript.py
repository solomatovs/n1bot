"""История треда: из langgraph-checkpointer'а в ленту chainlit тем же ChatView."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from boba.chainlit.agent.chat_model import ResponseField
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepText,
    TurnDraft,
)
from chainlit.step import StepDict

__all__ = [
    "CheckpointMessages",
    "ConversationTranscript",
    "PendingCall",
    "ThreadMessages",
    "TranscriptFeed",
    "TurnMark",
    "TurnRecord",
]


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


class TurnMark(StrEnum):
    """Исход хода в additional_kwargs: его читает сборка ленты из истории."""

    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TurnRecord:
    """Запись оборванного хода в историю агента."""

    content: str
    mark: TurnMark
    reasoning: str = ""

    def message(self) -> AIMessage:
        """Сообщение для состояния графа: пометка исхода и рассуждения при них."""
        extra: dict[str, Any] = {self.mark.value: True}
        if self.reasoning:
            extra[ResponseField.REASONING_CONTENT.value] = self.reasoning

        return AIMessage(content=self.content, additional_kwargs=extra)


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

    async def replay(self) -> None:
        for index, message in enumerate(self._messages):
            key = message.id
            if not key:
                key = f"#{index}"

            match message:
                case HumanMessage():
                    self._view.begin_turn(message.id)
                    self._pending.clear()
                    self._turn = TurnDraft(key=message.id)
                    await self._view.question(self._text(message), message.id)
                case ToolMessage():
                    await self._tool(message, key)
                case AIMessage():
                    await self._assistant(message, key)
                case _:
                    continue

    async def _assistant(self, message: AIMessage, key: str) -> None:
        if self._is_error(message):
            await self._view.error(self._text(message), self._answer_key(key))
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

    @staticmethod
    def _is_error(message: AIMessage) -> bool:
        extra = message.additional_kwargs
        if not extra:
            return False

        return bool(extra.get(TurnMark.ERROR.value))

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
