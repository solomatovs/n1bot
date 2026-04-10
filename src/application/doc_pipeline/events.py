"""События doc-пайплайна."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Union

from domain.doc_search import Fragment, SearchHit
from domain.pipeline import StageCompleted, StageStarted


# ---------------------------------------------------------------------------
# Индексация
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexingSkipped:
    """Индекс уже существует — пропускаем."""
    collection: str
    doc_count: int


@dataclass(frozen=True)
class FileIndexed:
    """Файл проиндексирован."""
    filename: str
    chunks: int
    index: int
    total: int


@dataclass(frozen=True)
class IndexingDone:
    """Индексация завершена."""
    total_files: int
    total_chunks: int


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchDone:
    """Поиск завершён — найдены чанки."""
    hits: List[SearchHit] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Чтение контекста
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextReady:
    """Расширенный контекст собран из файлов."""
    context: str
    fragments: List[Fragment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Генерация
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThinkingToken:
    """Токен размышления (стриминг)."""
    token: str


@dataclass(frozen=True)
class AnswerToken:
    """Токен ответа (стриминг)."""
    token: str


@dataclass(frozen=True)
class GenerationDone:
    """Генерация завершена."""


# ---------------------------------------------------------------------------
# Объединённый тип
# ---------------------------------------------------------------------------

DocPipelineEvent = Union[
    StageStarted, StageCompleted,
    IndexingSkipped, FileIndexed, IndexingDone,
    SearchDone,
    ContextReady,
    ThinkingToken, AnswerToken, GenerationDone,
]
