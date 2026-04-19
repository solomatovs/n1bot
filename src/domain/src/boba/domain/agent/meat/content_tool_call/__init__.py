"""Эвристики «tool call пришёл JSON-текстом в ``content``».

Две стратегии одной проблемы:

- :mod:`.streaming` — потоково распознаёт префикс и стримит
  ``ToolCallArgumentDelta`` по мере прихода чанков. Толерантна к
  лишнему тексту после JSON — «хвост» уйдёт обычным ``AnswerToken``.
- :mod:`.strict` — буферизует весь ``content`` и парсит целиком со
  строгим whitelist полей. Нарушения формата уходят обратно в LLM
  критикой через :class:`AgentErrorRouter`.
"""

from .streaming import (
    JsonContentToolCallMiddleware,
    JsonDepthScanner,
    JsonHeaderParser,
    JsonToolCallHeader,
)
from .strict import (
    ParsedJsonToolCall,
    StrictJsonContentToolCallMiddleware,
    StrictJsonToolCallParser,
)

__all__ = [
    "JsonContentToolCallMiddleware",
    "JsonDepthScanner",
    "JsonHeaderParser",
    "JsonToolCallHeader",
    "ParsedJsonToolCall",
    "StrictJsonContentToolCallMiddleware",
    "StrictJsonToolCallParser",
]
