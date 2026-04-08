"""События load-пайплайна — специфичные для загрузки документов."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Union

from langchain_core.documents import Document

from domain.pipeline.events import StageCompleted, StageStarted


# ---------------------------------------------------------------------------
# Загрузка страниц
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpaceEnumerated:
    """Получен список страниц пространства."""
    space_key: str
    total: int


@dataclass(frozen=True)
class PageLoaded:
    """Страница загружена."""
    page_id: str
    documents: List[Document]
    index: int
    total: int


@dataclass(frozen=True)
class PageFailed:
    """Не удалось загрузить страницу."""
    page_id: str
    error: Exception
    index: int
    total: int


@dataclass(frozen=True)
class LoadingDone:
    """Загрузка страниц завершена."""
    ok_count: int
    failed_count: int


# ---------------------------------------------------------------------------
# Чанкинг
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionChunked:
    """Обработана секция документа."""
    doc_index: int
    doc_total: int
    section_index: int
    section_total: int
    section_title: str


@dataclass(frozen=True)
class ChunkProduced:
    """Создан чанк."""
    chunk: Document
    cumulative_chunks: int


@dataclass(frozen=True)
class ChunkingDone:
    """Чанкинг завершён."""
    total_chunks: int


# ---------------------------------------------------------------------------
# Сохранение в ChromaDB
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreBatchDone:
    """Батч сохранён."""
    batch_index: int
    total_stored: int
    total_chunks: int


@dataclass(frozen=True)
class StoreBatchFailed:
    """Батч не удалось сохранить."""
    batch_index: int
    error: str
    total_failed: int
    total_chunks: int


@dataclass(frozen=True)
class StorageDone:
    """Сохранение завершено."""
    total_stored: int
    total_failed: int


# ---------------------------------------------------------------------------
# Объединённый тип всех событий load-пайплайна
# ---------------------------------------------------------------------------

LoadPipelineEvent = Union[
    StageStarted, StageCompleted,
    SpaceEnumerated, PageLoaded, PageFailed, LoadingDone,
    SectionChunked, ChunkProduced, ChunkingDone,
    StoreBatchDone, StoreBatchFailed, StorageDone,
]
