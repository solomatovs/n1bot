"""Поиск по базе знаний: ядро индекса и инструменты поиска."""

from boba.chainlit2.agent.tools.kb.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.chainlit2.agent.tools.kb.models import KnowledgeBaseError, SearchHit
from boba.chainlit2.agent.tools.kb.tools import KbTools, build_kb_tools

__all__ = [
    "KbTools",
    "KnowledgeBaseError",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "SearchHit",
    "build_kb_tools",
]
