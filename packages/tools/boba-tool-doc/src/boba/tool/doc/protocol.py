"""Контракт doc-payload'а: запросы на stdin, ответы маркерной строкой в stdout."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.doc.liteparse.protocol import ParseParams

__all__ = [
    "DocOutlineAnswer",
    "DocOutlineRow",
    "DocOutlineTrailer",
    "DocPagesAnswer",
    "DocPagesRequest",
    "DocPagesTrailer",
    "DocParams",
    "DocPathRequest",
    "DocSearchAnswer",
    "DocSearchRequest",
    "DocSearchRow",
    "DocSearchTrailer",
    "DocWindowAnswer",
    "DocWindowRequest",
    "DocWindowTrailer",
]


class DocParams(ParseParams):
    """Настройки парсера плюс лимит выдачи, который есть только у doc."""

    max_text_chars: int = Field(ge=1, description="Лимит длины текста в ответе.")


class DocRequest(BaseModel):
    """Общая часть запроса: что читаем и с какими настройками парсера."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1, description="Операция payload'а.")
    path: str = Field(min_length=1, description="Путь к файлу внутри песочницы.")
    params: DocParams = Field(description="Настройки парсера и лимит текста.")


class DocPathRequest(DocRequest):
    """Запрос без дополнительных полей: document_outline."""

    OUTLINE: ClassVar[str] = "document_outline"


class DocPagesRequest(DocRequest):
    """Запрос текста выбранных страниц."""

    OP: ClassVar[str] = "read_document"

    pages: str = Field(min_length=1, description="Страницы 1-based: '1-5,10'.")


class DocWindowRequest(DocRequest):
    """Запрос среза текста [start_char, start_char+length)."""

    OP: ClassVar[str] = "read_document_window"

    start_char: int = Field(ge=0, description="Начало окна в символах.")
    length: int = Field(ge=1, description="Длина окна в символах.")


class DocSearchRequest(DocRequest):
    """Запрос поиска фразы с координатами совпадений."""

    OP: ClassVar[str] = "search_document"

    query: str = Field(min_length=1, description="Искомая фраза.")
    context_chars: int = Field(ge=0, description="Контекст вокруг совпадения.")
    max_matches: int = Field(ge=1, description="Максимум совпадений в ответе.")


class DocPagesTrailer(BaseModel):
    """Итог чтения страниц: текст ушёл кадрами, здесь признаки и номера страниц."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
    pages: tuple[int, ...]


class DocWindowTrailer(BaseModel):
    """Итог окна: срез ушёл кадрами, здесь позиция курсора."""

    model_config = ConfigDict(extra="forbid")

    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    has_more: bool


class DocOutlineTrailer(BaseModel):
    """Итог карты документа: строки ушли кадрами."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)


class DocSearchTrailer(BaseModel):
    """Итог поиска: совпадения ушли кадрами."""

    model_config = ConfigDict(extra="forbid")

    limit_reached: bool


class DocPagesAnswer(BaseModel):
    """Текст выбранных страниц и номера тех, что действительно распарсились."""

    model_config = ConfigDict(extra="forbid")

    text: str
    truncated: bool
    pages: tuple[int, ...]


class DocWindowAnswer(BaseModel):
    """Срез текста и позиция курсора для следующего окна."""

    model_config = ConfigDict(extra="forbid")

    text: str
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    has_more: bool


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
