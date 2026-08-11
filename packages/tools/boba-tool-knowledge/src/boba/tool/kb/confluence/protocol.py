"""Контракт confluence-payload'а: запрос к REST делает песочница.

Приложение отдаёт базовый URL и профиль соединения, обратно получает готовый
текст или строки таблицы — ни сырого JSON, ни исходной разметки здесь нет.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.tool.doc.liteparse.protocol import ParseParams
from boba.toolkit.secrets import SecretDump
from boba.transport.http import HttpProfile

__all__ = [
    "ConfluenceAttachmentRequest",
    "ConfluenceContentCall",
    "ConfluenceGrepRequest",
    "ConfluenceGrepRow",
    "ConfluenceNode",
    "ConfluencePageRequest",
    "ConfluencePageTrailer",
    "ConfluenceSearchHit",
    "ConfluenceSearchRequest",
    "ConfluenceSpace",
    "ConfluenceSpacesRequest",
]


class ConfluenceNode(StrEnum):
    """Узлы реестра стадий над Confluence; значение — оно же поле op запроса."""

    PAGE = "confluence_page"
    GREP = "confluence_grep"
    SEARCH = "confluence_search"
    SPACES = "confluence_spaces"
    ATTACHMENT = "confluence_attachment"
    INGEST = "confluence_ingest"


class ConfluenceCall(BaseModel):
    """Общая часть: куда идти и с каким профилем."""

    model_config = ConfigDict(extra="forbid")

    op: ConfluenceNode
    base_url: str = Field(min_length=1)
    profile: HttpProfile

    @field_serializer("profile", when_used="json")
    def _dump_profile(self, value: HttpProfile) -> dict[str, Any]:
        """tool_args песочницы — доверенный канал: только здесь креды раскрыты."""
        return SecretDump.of(value)


class ConfluencePageCall(ConfluenceCall):
    """Общая часть операций над одной страницей: её адрес в Confluence."""

    page_id: str = Field(min_length=1)
    body_format: str = Field(min_length=1)


class ConfluenceContentCall(ConfluencePageCall):
    """Общая часть операций над контентом страницы."""

    as_markdown: bool


class ConfluencePageRequest(ConfluenceContentCall):
    """Скачать одну страницу."""


class ConfluencePageTrailer(BaseModel):
    """Итог страницы: контент ушёл потоком, здесь её заголовок."""

    model_config = ConfigDict(extra="forbid")

    title: str


class ConfluenceGrepRequest(ConfluenceContentCall):
    """Поиск по содержимому одной страницы."""

    pattern: str = Field(min_length=1)
    case_insensitive: bool
    context: int = Field(ge=0)
    limit: int = Field(ge=1)
    fixed_string: bool
    max_text_chars: int = Field(ge=1)


class ConfluenceGrepRow(BaseModel):
    """Совпадение со строками контекста."""

    model_config = ConfigDict(extra="forbid")

    line: int = Field(ge=1)
    content: str
    before: tuple[str, ...]
    after: tuple[str, ...]


class ConfluenceSearchRequest(ConfluenceCall):
    """Поиск страниц по CQL."""

    cql: str = Field(min_length=1)
    limit: int = Field(ge=1)
    snippet_chars: int = Field(ge=1)


class ConfluenceSearchHit(BaseModel):
    """Найденная страница: чем она является и где лежит."""

    model_config = ConfigDict(extra="forbid")

    page_id: str
    title: str
    space_key: str
    url: str
    excerpt: str


class ConfluenceSpacesRequest(ConfluenceCall):
    """Список пространств."""

    space_type: str = Field(min_length=1)
    limit: int = Field(ge=1)


class ConfluenceSpace(BaseModel):
    """Пространство Confluence."""

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    type: str


class ConfluenceAttachmentRequest(ConfluencePageCall):
    """Скачать вложение страницы и распарсить его.

    Настройки парсера лежат полями: их сводка ошибок точнее вложенной модели,
    а OCR-ручки вызова и значения конфига попадают в запрос одинаково.
    """

    filename: str = Field(min_length=1)
    ocr_enabled: bool = Field(description="Распознавать текст по картинкам.")
    ocr_language: str = Field(
        min_length=1,
        description="Язык OCR в формате Tesseract: 'rus+eng', 'eng', 'rus'.",
    )
    max_pages: int = Field(ge=0, description="Лимит страниц; 0 — без лимита.")
    tessdata_path: str = Field(
        min_length=1,
        description="Каталог моделей OCR внутри песочницы.",
    )
    num_workers: int = Field(ge=1, description="Параллелизм OCR.")

    def parse_params(self) -> ParseParams:
        """Настройки движка liteparse из полей запроса."""
        return ParseParams(
            ocr_enabled=self.ocr_enabled,
            ocr_language=self.ocr_language,
            max_pages=self.max_pages,
            tessdata_path=self.tessdata_path,
            num_workers=self.num_workers,
        )
