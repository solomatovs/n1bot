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


# ---------------------------------------------------------------------------
# Индексация
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Хранилище thread'ов
# ---------------------------------------------------------------------------


class ThreadStoreError(AppError):
    """Базовая ошибка работы с хранилищем thread'ов."""



class ThreadNotFoundError(ThreadStoreError):
    """Thread не найден по ID."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Thread not found: {thread_id}")


class ThreadReadError(ThreadStoreError):
    """Не удалось прочитать thread.json (битый JSON, ошибка IO)."""

    def __init__(self, path: str, reason: Exception) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Failed to read thread {path}: {reason}")


class ThreadWriteError(ThreadStoreError):
    """Не удалось записать thread.json."""

    def __init__(self, path: str, reason: Exception) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Failed to write thread {path}: {reason}")


class ThreadDeleteError(ThreadStoreError):
    """Не удалось удалить workspace."""

    def __init__(self, thread_id: str, reason: Exception) -> None:
        self.thread_id = thread_id
        self.reason = reason
        super().__init__(f"Failed to delete workspace for thread {thread_id}: {reason}")


# ---------------------------------------------------------------------------
# Индексация
# ---------------------------------------------------------------------------


class CorruptedIndexError(AppError):
    """Индексный документ не содержит обязательных метаданных.

    Необходимо выполнить переиндексацию.
    """

    def __init__(self, missing_fields: list[str], source_file: str = "") -> None:
        self.missing_fields = missing_fields
        self.source_file = source_file
        fields = ", ".join(missing_fields)
        ctx = f" (документ: {source_file})" if source_file else ""
        super().__init__(
            f"Индекс повреждён: отсутствуют поля [{fields}]{ctx}. "
            f"Удалите папку .boba и выполните переиндексацию."
        )
