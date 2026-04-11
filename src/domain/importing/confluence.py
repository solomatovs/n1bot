"""Доменные типы для работы с Confluence — события, протокол, query-параметры."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Protocol, Union, runtime_checkable

from domain.importing.loading import SpaceLoadParams


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


def extract_page_ids(response_json: dict) -> List[str]:
    """Извлечь page_id из ответа Confluence REST API."""
    results = response_json.get("results") or []
    return [str(item["id"]) for item in results if "id" in item]


def extract_page_title(page_json: dict, fallback: str = "") -> str:
    """Извлечь title страницы из ответа Confluence REST API."""
    return page_json.get("title", fallback)


def extract_export_view_html(page_json: dict) -> str:
    """Извлечь HTML-контент (export_view) из ответа Confluence REST API."""
    return page_json["body"]["export_view"]["value"]
