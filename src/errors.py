"""Иерархия типизированных ошибок приложения."""
from __future__ import annotations


class AppError(Exception):
    """Базовая ошибка приложения."""


# ---------------------------------------------------------------------------
# RAG-пайплайн
# ---------------------------------------------------------------------------

class RagError(AppError):
    """Ошибка RAG-пайплайна."""


class EmptyContextError(RagError):
    """Не найдено релевантных документов в векторной базе."""


class LLMGenerationError(RagError):
    """Ошибка при генерации ответа моделью."""


# ---------------------------------------------------------------------------
# Поиск (retrieval)
# ---------------------------------------------------------------------------

class RetrievalError(AppError):
    """Ошибка поиска документов в векторном хранилище."""


class QueryVariantError(RetrievalError):
    """Ошибка при генерации переформулировок запроса."""


# ---------------------------------------------------------------------------
# Векторное хранилище
# ---------------------------------------------------------------------------

class VectorStoreError(AppError):
    """Ошибка работы с векторным хранилищем."""


class EmbeddingConnectionError(VectorStoreError):
    """Не удалось подключиться к сервису эмбеддингов."""


class DocumentStorageError(VectorStoreError):
    """Ошибка при сохранении документов в хранилище."""


# ---------------------------------------------------------------------------
# Загрузка из Confluence
# ---------------------------------------------------------------------------

class IngestionError(AppError):
    """Ошибка при импорте данных."""


class PageLoadError(IngestionError):
    """Не удалось загрузить страницу из Confluence."""

    def __init__(self, page_id: str, cause: Exception) -> None:
        self.page_id = page_id
        self.cause = cause
        super().__init__(f"Не удалось загрузить страницу {page_id}: {cause}")


class SpaceEnumerationError(IngestionError):
    """Не удалось получить список страниц пространства."""

    def __init__(self, space_key: str, cause: Exception) -> None:
        self.space_key = space_key
        self.cause = cause
        super().__init__(f"Не удалось перечислить страницы пространства '{space_key}': {cause}")
