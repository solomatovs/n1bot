"""Загрузка — параметры импорта из Confluence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from boba_domain.errors import AppError


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


# ---------------------------------------------------------------------------
# Параметры
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfluenceImportParams:
    """Параметры импорта страниц из Confluence на диск."""
    timeout: int = 20
    ssl_verify: bool = False
    token: Optional[str] = None


@dataclass
class SpaceLoadParams:
    """Параметры загрузки пространства Confluence."""
    api_page_limit: int = 50
    max_pages: Optional[int] = None
