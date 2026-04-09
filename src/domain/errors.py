"""Базовые ошибки приложения и инфраструктурные ошибки."""
from __future__ import annotations


class AppError(Exception):
    """Базовая ошибка приложения."""


class ValidationError(AppError):
    """Обязательный параметр отсутствует или некорректен."""


# ---------------------------------------------------------------------------
# Векторное хранилище
# ---------------------------------------------------------------------------

class VectorStoreError(AppError):
    """Базовая ошибка работы с векторным хранилищем."""


class CollectionNotFoundError(VectorStoreError):
    """Коллекция не найдена."""

    def __init__(self, collection_name: str) -> None:
        self.collection_name = collection_name
        super().__init__(f"Collection not found: {collection_name}")


class CollectionCreateError(VectorStoreError):
    """Не удалось создать коллекцию."""


class StoreBatchError(VectorStoreError):
    """Не удалось сохранить батч документов."""


class SearchError(VectorStoreError):
    """Ошибка поиска по коллекции."""


class VectorStoreConnectionError(VectorStoreError):
    """Не удалось подключиться к векторному хранилищу."""
