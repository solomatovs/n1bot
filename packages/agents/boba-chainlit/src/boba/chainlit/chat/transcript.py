"""История треда: из langgraph-checkpointer'а в ленту chainlit тем же ChatView."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from boba.chainlit.rendering.chat_view import ChatView

__all__ = ["CheckpointMessages", "ConversationTranscript", "ThreadMessages"]


class ThreadMessages(Protocol):
    """Источник сообщений треда, из которых собирается лента."""

    async def load(self, thread_id: str) -> list[BaseMessage]: ...


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
        self._pending: dict[str, Mapping[str, Any]] = {}
        self._turn_key: str | None = None
        self._answers = 0

    async def replay(self) -> None:
        for index, message in enumerate(self._messages):
            key = message.id or f"#{index}"
            match message:
                case HumanMessage():
                    self._view.begin_turn(message.id)
                    self._pending.clear()
                    self._turn_key, self._answers = message.id, 0
                    await self._view.question(self._text(message), message.id)
                case ToolMessage():
                    await self._tool(message, key)
                case AIMessage():
                    await self._assistant(message, key)
                case _:
                    continue

    async def _assistant(self, message: AIMessage, key: str) -> None:
        if self._is_error(message):
            await self._view.error(self._text(message), self._answer_key() or key)
            return

        if reasoning := self._reasoning(message):
            await self._view.thinking(reasoning, key)

        for call in message.tool_calls or ():
            if call_id := call.get("id"):
                self._pending[call_id] = call

        if text := self._text(message):
            await self._view.answer(text, self._answer_key() or key)

    async def _tool(self, message: ToolMessage, key: str) -> None:
        call = self._pending.pop(message.tool_call_id, None)
        if call is None:
            call = {}

        name = message.name
        if not name:
            name = call.get("name")
        if not name:
            name = "tool"

        args = cast("Mapping[str, Any] | None", call.get("args"))
        step = await self._view.tool_started(
            str(name), args, message.tool_call_id or key
        )

        if message.status == "error":
            await self._view.tool_failed(step, self._text(message))
            return

        artifact = message.artifact
        if artifact is None:
            artifact = self._text(message)
        await self._view.tool_finished(step, artifact, message.tool_call_id)

    def _answer_key(self) -> str | None:
        if self._turn_key is None:
            return None
        suffix = ""
        if self._answers:
            suffix = f"#{self._answers}"
        key = f"{self._turn_key}{suffix}"
        self._answers += 1
        return key

    @staticmethod
    def _is_error(message: AIMessage) -> bool:
        return bool((message.additional_kwargs or {}).get("error"))

    @staticmethod
    def _reasoning(message: AIMessage) -> str:
        value = (message.additional_kwargs or {}).get("reasoning_content")
        if value:
            return str(value)
        return ""

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
