"""Доменные типы для работы с Confluence — события, протокол, query-параметры."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Protocol, Union, runtime_checkable

from boba_domain.importing.loading import ConfluenceImportParams, SpaceLoadParams


@dataclass(frozen=True)
class ImportPageSaved:
    """Страница импортирована на диск."""
    page_id: str
    title: str
    file_path: str
    index: int
    total: int


@dataclass(frozen=True)
class ImportPageFailed:
    """Не удалось импортировать страницу."""
    page_id: str
    error: str
    index: int
    total: int


@dataclass(frozen=True)
class ImportSpaceEnumerated:
    """Получен список страниц пространства для импорта."""
    space_key: str
    total: int


@dataclass(frozen=True)
class ImportDone:
    """Импорт завершён."""
    ok_count: int
    failed_count: int
    output_dir: str


ImportEvent = Union[ImportPageSaved, ImportPageFailed, ImportSpaceEnumerated, ImportDone]


@runtime_checkable
class ConfluenceImportService(Protocol):
    """Импорт страниц из Confluence на диск."""

    def import_pages(
        self, page_ids: List[str], output_dir: Path,
    ) -> Iterator[ImportEvent]: ...

    def import_space(
        self, space_key: str, space_params: SpaceLoadParams, output_dir: Path,
    ) -> Iterator[ImportEvent]: ...

    def close(self) -> None: ...


class ConfluenceImportFactory(Protocol):
    """Фабрика для создания ConfluenceImportService с runtime-параметрами."""

    def __call__(self, params: ConfluenceImportParams) -> ConfluenceImportService: ...


# ---------------------------------------------------------------------------
# Общие типы для Confluence REST API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfluencePageQuery:
    """Параметры запроса одной страницы Confluence."""
    expand: str = "body.export_view"

    def to_params(self) -> dict[str, str]:
        return {"expand": self.expand}


@dataclass(frozen=True)
class ConfluenceSpaceQuery:
    """Параметры запроса пагинации страниц пространства Confluence."""
    space_key: str
    content_type: str = "page"
    limit: int = 50
    start: int = 0

    def to_params(self) -> dict[str, str | int]:
        return {
            "spaceKey": self.space_key,
            "type": self.content_type,
            "limit": self.limit,
            "start": self.start,
        }
