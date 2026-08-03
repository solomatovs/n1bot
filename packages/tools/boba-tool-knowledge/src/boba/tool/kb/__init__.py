"""Поиск по базе знаний: ядро индекса и инструменты поиска."""

from boba.tool.kb.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.tools import KbTools, build_kb_tools

__all__ = [
    "KbTools",
    "KnowledgeBaseError",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "SearchHit",
    "build_kb_tools",
]
