"""События query-пайплайна — специфичные для RAG-поиска и генерации."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Union

from domain.pipeline import StageCompleted, StageStarted


@dataclass(frozen=True)
class QueryVariantsGenerated:
    """Сгенерированы переформулировки запроса."""
    variants: List[str]


@dataclass(frozen=True)
class RetrievalStarted:
    """Начат поиск по векторной базе."""
    query: str
    collection: str


@dataclass(frozen=True)
class RetrievalDone:
    """Поиск завершён, найдены документы."""
    documents_found: int
    context: str
    sources_block: str


@dataclass(frozen=True)
class ThinkingToken:
    """Токен размышления от LLM (reasoning_content или <think>)."""
    token: str


@dataclass(frozen=True)
class AnswerToken:
    """Токен ответа от LLM."""
    token: str


@dataclass(frozen=True)
class GenerationDone:
    """Генерация ответа завершена."""


# Объединённый тип всех событий query-пайплайна
ChatEvent = Union[
    RetrievalStarted,
    RetrievalDone,
    ThinkingToken,
    AnswerToken,
    GenerationDone,
    StageStarted,
    StageCompleted,
    QueryVariantsGenerated,
]
