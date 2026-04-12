"""ContextWindow — управляемое контекстное окно LLM.

Append-only коллекция LLMMessage с автоматическим compaction.
Единственный владелец состояния messages — мутации только через методы.

Чистый domain-тип. Конвертация в API-формат — через адаптер снаружи.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from boba_domain.chat.messages import LLMMessage, LLMRole

log = logging.getLogger(__name__)


class ContextWindow:
    """Управляемое контекстное окно LLM.

    Хранит messages и tool_definitions как domain-типы.
    Конвертация в OpenAI API формат — ответственность адаптера.
    """

    def __init__(self, *, max_chars: int = 400_000) -> None:
        self._messages: List[LLMMessage] = []
        self._tool_definitions: List[Dict[str, Any]] = []
        self._max_chars = max_chars
        self._COMPACT_KEEP_CHARS = 500
        self._COMPACT_SUFFIX = "\n\n... (усечено)"
        self._COMPACT_THRESHOLD = 0.85

    # -----------------------------------------------------------------------
    # Мутации
    # -----------------------------------------------------------------------

    def add_system(self, content: str) -> None:
        self._messages.append(LLMMessage(role=LLMRole.SYSTEM, content=content))

    def add_user(self, content: str) -> None:
        self._messages.append(LLMMessage(role=LLMRole.USER, content=content))

    def add_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        self._messages.append(LLMMessage(role=LLMRole.ASSISTANT, tool_calls=tool_calls))

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append(LLMMessage(
            role=LLMRole.TOOL, content=content, tool_call_id=tool_call_id,
        ))
        self._maybe_compact()

    def set_tool_definitions(self, definitions: List[Dict[str, Any]]) -> None:
        self._tool_definitions = definitions

    # -----------------------------------------------------------------------
    # Доступ к данным (domain-типы)
    # -----------------------------------------------------------------------

    @property
    def messages(self) -> Sequence[LLMMessage]:
        """Все messages как domain-типы (read-only)."""
        return self._messages

    @property
    def tool_definitions(self) -> List[Dict[str, Any]]:
        """Tool definitions (JSON Schema формат)."""
        return self._tool_definitions

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def total_chars(self) -> int:
        return sum(len(m.content) for m in self._messages)

    # -----------------------------------------------------------------------
    # Compaction
    # -----------------------------------------------------------------------

    def _maybe_compact(self) -> None:
        threshold = int(self._max_chars * self._COMPACT_THRESHOLD)
        while self.total_chars > threshold:
            if not self._compact_oldest_tool_result():
                break

    def _compact_oldest_tool_result(self) -> bool:
        min_size = self._COMPACT_KEEP_CHARS + len(self._COMPACT_SUFFIX)
        for i, msg in enumerate(self._messages):
            if msg.role is not LLMRole.TOOL:
                continue
            if len(msg.content) <= min_size:
                continue
            compacted = msg.content[:self._COMPACT_KEEP_CHARS] + self._COMPACT_SUFFIX
            self._messages[i] = LLMMessage(
                role=msg.role, content=compacted, tool_call_id=msg.tool_call_id,
            )
            log.debug("Compacted tool result %s: %d → %d chars",
                      msg.tool_call_id, len(msg.content), len(compacted))
            return True
        return False
