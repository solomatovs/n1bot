"""ContextWindow — управляемое контекстное окно LLM.

Append-only коллекция LLM messages с автоматическим compaction.
Единственный владелец состояния messages — мутации только через методы.

Compaction: когда total_chars приближается к max_chars,
старые tool results обрезаются до _COMPACT_KEEP_CHARS символов.
System prompt и user messages не сжимаются.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from domain.chat.messages import LLMMessage, LLMRole

log = logging.getLogger(__name__)

_COMPACT_KEEP_CHARS = 500
_COMPACT_SUFFIX = "\n\n... (усечено)"
_COMPACT_THRESHOLD = 0.85  # начинать compaction при 85% заполнения


class ContextWindow:
    """Управляемое контекстное окно LLM.

    Добавление messages через типизированные методы.
    Автоматический compaction старых tool results при приближении к лимиту.
    """

    def __init__(self, *, max_chars: int = 400_000) -> None:
        self._messages: List[LLMMessage] = []
        self._max_chars = max_chars

    # -----------------------------------------------------------------------
    # Мутации
    # -----------------------------------------------------------------------

    def add_system(self, content: str) -> None:
        """Добавить system prompt."""
        self._messages.append(LLMMessage(role=LLMRole.SYSTEM, content=content))

    def add_user(self, content: str) -> None:
        """Добавить user message."""
        self._messages.append(LLMMessage(role=LLMRole.USER, content=content))

    def add_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        """Добавить assistant message с tool_calls."""
        self._messages.append(LLMMessage(role=LLMRole.ASSISTANT, tool_calls=tool_calls))

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Добавить tool result. Может запустить compaction."""
        self._messages.append(LLMMessage(
            role=LLMRole.TOOL, content=content, tool_call_id=tool_call_id,
        ))
        self._maybe_compact()

    # -----------------------------------------------------------------------
    # Snapshot для LLM API
    # -----------------------------------------------------------------------

    def to_messages(self) -> List[Dict[str, Any]]:
        """Snapshot messages для вызова chat.completions.create()."""
        return [m.to_dict() for m in self._messages]

    # -----------------------------------------------------------------------
    # Информация
    # -----------------------------------------------------------------------

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def total_chars(self) -> int:
        return sum(len(m.content) for m in self._messages)

    # -----------------------------------------------------------------------
    # Compaction — сжатие старых tool results
    # -----------------------------------------------------------------------

    def _maybe_compact(self) -> None:
        """Сжать старые tool results если приближаемся к лимиту."""
        threshold = int(self._max_chars * _COMPACT_THRESHOLD)
        while self.total_chars > threshold:
            if not self._compact_oldest_tool_result():
                break

    def _compact_oldest_tool_result(self) -> bool:
        """Найти самый старый несжатый tool result и обрезать.

        Returns:
            True если удалось сжать, False если нечего сжимать.
        """
        min_size = _COMPACT_KEEP_CHARS + len(_COMPACT_SUFFIX)
        for i, msg in enumerate(self._messages):
            if msg.role is not LLMRole.TOOL:
                continue
            if len(msg.content) <= min_size:
                continue
            # Нашли — обрезаем
            compacted = msg.content[:_COMPACT_KEEP_CHARS] + _COMPACT_SUFFIX
            self._messages[i] = LLMMessage(
                role=msg.role,
                content=compacted,
                tool_call_id=msg.tool_call_id,
            )
            log.debug(
                "Compacted tool result %s: %d → %d chars",
                msg.tool_call_id, len(msg.content), len(compacted),
            )
            return True
        return False
