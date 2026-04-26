"""Доменные ошибки KB-tools.

Все наследуются от :class:`ToolExecutionError` core-API — это значит,
``ToolExecutionMiddleware`` поймает их по базовому типу, запишет
``message`` в историю как ``role="tool"`` и эмитит
:class:`ToolExecutionFailed` для observability. Никакого специального
проброса не нужно.
"""

from __future__ import annotations

from boba.domain.core.tools import ToolExecutionError, ToolId


class KnowledgeBaseError(ToolExecutionError):
    """База для всех ошибок ChromaKnowledgeBase. Используется для
    обёртывания chromadb-исключений с понятным для агента сообщением.
    """


class CollectionNotFoundError(KnowledgeBaseError):
    """Коллекция с таким именем не зарегистрирована в БД. Агент увидит
    список доступных через ``kb_list_collections``.
    """

    def __init__(self, tool_id: ToolId, name: str) -> None:
        super().__init__(
            tool_id,
            f"collection {name!r} not found; "
            f"call kb_list_collections to see available ones",
        )
        self.name = name
