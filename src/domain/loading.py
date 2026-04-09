"""Загрузка — параметры и ошибки."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from domain.errors import AppError


# ---------------------------------------------------------------------------
# Ошибки загрузки
# ---------------------------------------------------------------------------

class PageLoadError(AppError):
    """Не удалось загрузить страницу из Confluence."""

    def __init__(self, page_id: str, cause: Exception) -> None:
        self.page_id = page_id
        self.cause = cause
        super().__init__(f"Failed to load page {page_id}: {cause}")


class SpaceEnumerationError(AppError):
    """Не удалось получить список страниц пространства."""

    def __init__(self, space_key: str, cause: Exception) -> None:
        self.space_key = space_key
        self.cause = cause
        super().__init__(f"Failed to enumerate pages in space '{space_key}': {cause}")


class ContentType(Enum):
    """Типы контента в векторном хранилище."""
    TEXT = "text", "Текст"
    CODE = "code", "Код"
    TABLE = "table", "Таблицы"
    PARAGRAPH = "paragraph", "Параграфы"
    LIST = "list", "Списки"

    def __init__(self, key: str, label: str) -> None:
        self._key = key
        self._label = label

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        return self._label

    @classmethod
    def labels_to_keys(cls, labels: List[str]) -> list[str] | None:
        """Преобразовать выбранные labels в ключи. Пустой список → None."""
        label_map = {ct.label: ct.key for ct in cls}
        keys = [label_map[lb] for lb in labels if lb in label_map]
        return keys or None


@dataclass
class ChunkingParams:
    """Параметры чанкинга документов."""
    enabled: bool = True
    max_tokens: int = 500
    similarity_threshold: float = 0.7
    embedding_timeout: int = 120
    code_ratio_threshold: float = 0.3
    table_ratio_threshold: float = 0.3
    list_ratio_threshold: float = 0.4


class ConfluenceContentFormat(Enum):
    """Формат контента страницы Confluence."""
    STORAGE = "body.storage", "Storage (XML)"
    VIEW = "body.view", "View (HTML)"
    EXPORT_VIEW = "body.export_view", "Export View"
    EDITOR = "body.editor", "Editor"
    ANONYMOUS_EXPORT_VIEW = "body.anonymous_export_view", "Anonymous Export View"

    def __init__(self, value: str, label: str) -> None:
        self._value_ = value
        self._label = label

    @property
    def label(self) -> str:
        return self._label


@dataclass(frozen=True)
class ConfluenceLoaderParams:
    """Параметры загрузки страниц из Confluence.

    Объединяет настройки контента, вложений и HTTP-транспорта.
    """

    # --- Формат контента ---
    content_format: ConfluenceContentFormat = ConfluenceContentFormat.EXPORT_VIEW
    keep_markdown_format: bool = True
    keep_newlines: bool = False

    # --- Включение дополнительного контента ---
    include_attachments: bool = False
    include_comments: bool = False
    include_labels: bool = False

    # --- Retry-политика ---
    number_of_retries: int = 3
    min_retry_seconds: int = 2
    max_retry_seconds: int = 10

    # --- HTTP-транспорт ---
    timeout: int = 20
    ssl_verify: bool = False


@dataclass
class SpaceLoadParams:
    """Параметры загрузки пространства Confluence."""
    api_page_limit: int = 50
    max_pages: Optional[int] = None


@dataclass
class StorageParams:
    """Параметры сохранения в ChromaDB."""
    batch_size: int = 32


BATCH_SIZE_OPTIONS = [8, 16, 32, 64, 128]
