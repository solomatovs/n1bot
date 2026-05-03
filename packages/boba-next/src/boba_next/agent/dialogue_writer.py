"""DialogueWriter — единственный писатель в историю сообщений."""

from __future__ import annotations

from collections.abc import Iterable

from boba_next.agent.messages import MessageWriter
from boba_next.llm.models import LLMMessage, LLMToolCall


class DialogueWriter:
    """Доменный фасад поверх MessageWriter."""

    def __init__(self, writer: MessageWriter) -> None:
        self._writer = writer

    def append_user_query(self, content: str) -> None:
        """Первое сообщение пользователя."""
        self._writer.add(LLMMessage(role="user", content=content))

    def append_assistant(
        self,
        content: str,
        tool_calls: Iterable[LLMToolCall],
    ) -> None:
        """Ответ модели после GenerationDone."""
        self._writer.add(
            LLMMessage(
                role="assistant",
                content=content,
                tool_calls=tuple(tool_calls),
            ),
        )

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        """Результат выполнения tool_call (role="tool")."""
        self._writer.add(
            LLMMessage(
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
            ),
        )

    def append_llm_critique(self, content: str) -> None:
        """Общая критика к LLM (role="user"), не привязана к tool_call."""
        self._writer.add(LLMMessage(role="user", content=content))

    def append_tool_call_rejection(
        self,
        *,
        tool_call_id: str,
        content: str,
    ) -> None:
        """Подавление tool_call'а: синтетический ответ в его слот."""
        self._writer.add(
            LLMMessage(role="tool", content=content, tool_call_id=tool_call_id),
        )
