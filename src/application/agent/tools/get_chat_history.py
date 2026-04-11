"""Tool: получение истории предыдущих сообщений чата."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator

from domain.workspace import Workspace
from domain.agent.events import DocPipelineEvent
from domain.chat.history import EventType, JsonlChatReader
from domain.agent.tools import Tool, ToolOutput, ToolResult

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class ChatHistoryParams:
    """Параметры получения истории чата."""
    last_n: int = field(default=10, metadata={"description": "Количество последних сообщений (по умолчанию 10)"})


class GetChatHistoryTool(Tool[DocPipelineEvent, ChatHistoryParams]):
    """Получение истории предыдущих сообщений чата."""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    @property
    def name(self) -> str:
        return "get_chat_history"

    @property
    def description(self) -> str:
        return (
            "Получить историю предыдущих сообщений в этом чате. "
            "Используй, когда нужен контекст предыдущего разговора — "
            "например, если пользователь ссылается на ранее обсуждённое."
        )

    @property
    def params_type(self) -> type[ChatHistoryParams]:
        return ChatHistoryParams

    def execute(self, params: ChatHistoryParams) -> Iterator[DocToolOutput]:
        if self._ws.history_path is None or not self._ws.history_path.exists():
            yield ToolResult(content="История чата пуста.")
            return

        messages: list[str] = []
        with JsonlChatReader(self._ws.history_path) as reader:
            for event in reader.read():
                if event.event_type is EventType.USER:
                    messages.append(f"[user] {event.content}")
                elif event.event_type is EventType.ASSISTANT:
                    messages.append(f"[assistant] {event.content}")
                elif event.event_type is EventType.TOOL_CALL:
                    tool_name = event.metadata.get("tool_name", "?")
                    messages.append(f"[tool_call] {tool_name}({event.content})")
                elif event.event_type is EventType.TOOL_RESULT:
                    tool_name = event.metadata.get("tool_name", "?")
                    messages.append(f"[tool_result:{tool_name}] {event.content[:500]}")

        if not messages:
            yield ToolResult(content="История чата пуста.")
            return

        tail = messages[-params.last_n:]
        yield ToolResult(
            content=f"История чата (последние {len(tail)} из {len(messages)}):\n\n"
            + "\n\n".join(tail),
        )
