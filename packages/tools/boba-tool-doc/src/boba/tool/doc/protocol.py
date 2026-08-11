"""Контракт doc-узлов: запросы, настроечная часть узла, квитанции и ответы.

Ошибки: pydantic.ValidationError — при разборе моделей контракта.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.doc.liteparse.protocol import ParseRequest

__all__ = [
    "DocOp",
    "DocOutlineAnswer",
    "DocOutlineRow",
    "DocOutlineTrailer",
    "DocPagesAnswer",
    "DocPagesRequest",
    "DocPagesTrailer",
    "DocPathRequest",
    "DocReadSettings",
    "DocSearchAnswer",
    "DocSearchRequest",
    "DocSearchRow",
    "DocSearchSettings",
    "DocSearchTrailer",
    "DocSettings",
]


class DocOp(StrEnum):
    """Операции doc-payload'а; они же имена узлов реестра стадий."""

    READ = "read_document"
    OUTLINE = "document_outline"
    SEARCH = "search_document"


class DocRequest(ParseRequest):
    """Общая часть запроса: что читаем и с какими настройками парсера."""

    path: str = Field(min_length=1, description="Путь к файлу внутри песочницы.")


class DocPathRequest(DocRequest):
    """Запрос без дополнительных полей: document_outline."""


class DocPagesRequest(DocRequest):
    """Запрос текста выбранных страниц."""

    pages: str = Field(min_length=1, description="Страницы 1-based: '1-5,10'.")
    max_text_chars: int = Field(ge=1, description="Лимит длины текста в ответе.")


class DocSearchRequest(DocRequest):
    """Запрос поиска фразы с координатами совпадений."""

    query: str = Field(min_length=1, description="Искомая фраза.")
    context_chars: int = Field(ge=0, description="Контекст вокруг совпадения.")
    max_matches: int = Field(ge=1, description="Максимум совпадений в ответе.")


class DocSettings(BaseModel):
    """Часть запроса doc-узла, которую задаёт конфиг, а не вызывающий."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    max_pages: int = Field(ge=0)
    tessdata_path: str = Field(min_length=1)


class DocReadSettings(DocSettings):
    """Настройки read_document: лимит длины текста берётся из конфига."""

    max_text_chars: int = Field(ge=1)


class DocSearchSettings(DocSettings):
    """Настройки search_document: контекст и потолок числа совпадений."""

    context_chars: int = Field(ge=0)
    max_matches: int = Field(ge=1)


class DocPagesTrailer(BaseModel):
    """Итог чтения страниц: текст ушёл каналом данных, здесь признаки и номера."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
    pages: tuple[int, ...]


class DocOutlineTrailer(BaseModel):
    """Итог карты документа: строки ушли каналом данных."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)


class DocSearchTrailer(BaseModel):
    """Итог поиска: совпадения ушли каналом данных."""

    model_config = ConfigDict(extra="forbid")

    limit_reached: bool


class DocPagesAnswer(BaseModel):
    """Текст выбранных страниц и номера тех, что действительно распарсились."""

    model_config = ConfigDict(extra="forbid")

    text: str
    truncated: bool
    pages: tuple[int, ...]


class DocOutlineRow(BaseModel):
    """Строка карты документа: одна страница."""

    model_config = ConfigDict(extra="forbid")

    page: int
    width: float
    height: float
    chars: int
    items: int


class DocOutlineAnswer(BaseModel):
    """Карта документа по страницам."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)
    rows: tuple[DocOutlineRow, ...]


class DocSearchRow(BaseModel):
    """Одно совпадение: страница, координаты рамки и сниппет."""

    model_config = ConfigDict(extra="forbid")

    page: int
    x: float
    y: float
    width: float
    height: float
    snippet: str


class DocSearchAnswer(BaseModel):
    """Совпадения и признак того, что упёрлись в max_matches."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[DocSearchRow, ...]
    limit_reached: bool
