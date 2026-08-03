"""Контракт confluence-payload'а: запрос к REST делает песочница.

Приложение отдаёт базовый URL и профиль соединения, обратно получает готовый
текст или строки таблицы — ни сырого JSON, ни исходной разметки здесь нет.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.doc.liteparse.protocol import ParseParams
from boba.tool.web.protocol import WebProfile

__all__ = [
    "ConfluenceAttachmentAnswer",
    "ConfluenceAttachmentRequest",
    "ConfluenceGrepAnswer",
    "ConfluenceGrepRequest",
    "ConfluencePageAnswer",
    "ConfluencePageRequest",
    "ConfluenceSearchAnswer",
    "ConfluenceSearchHit",
    "ConfluenceSearchRequest",
    "ConfluenceSpace",
    "ConfluenceSpacesAnswer",
    "ConfluenceSpacesRequest",
]


class ConfluenceCall(BaseModel):
    """Общая часть: куда идти и с каким профилем."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    profile: WebProfile


class ConfluencePageRequest(ConfluenceCall):
    """Скачать одну страницу."""

    OP: ClassVar[str] = "confluence_page"

    page_id: str = Field(min_length=1)
    body_format: str = Field(min_length=1)
    as_markdown: bool


class ConfluencePageAnswer(BaseModel):
    """Контент страницы и её заголовок."""

    model_config = ConfigDict(extra="forbid")

    text: str
    title: str


class ConfluenceGrepRequest(ConfluencePageRequest):
    """Поиск по содержимому одной страницы."""

    OP: ClassVar[str] = "confluence_grep"

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


class ConfluenceGrepAnswer(BaseModel):
    """Найденные совпадения."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[ConfluenceGrepRow, ...]


class ConfluenceSearchRequest(ConfluenceCall):
    """Поиск страниц по CQL."""

    OP: ClassVar[str] = "confluence_search"

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


class ConfluenceSearchAnswer(BaseModel):
    """Результаты поиска по порядку выдачи Confluence."""

    model_config = ConfigDict(extra="forbid")

    hits: tuple[ConfluenceSearchHit, ...]


class ConfluenceSpacesRequest(ConfluenceCall):
    """Список пространств."""

    OP: ClassVar[str] = "confluence_spaces"

    space_type: str = Field(min_length=1)
    limit: int = Field(ge=1)


class ConfluenceSpace(BaseModel):
    """Пространство Confluence."""

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    type: str


class ConfluenceSpacesAnswer(BaseModel):
    """Пространства, доступные учётной записи."""

    model_config = ConfigDict(extra="forbid")

    spaces: tuple[ConfluenceSpace, ...]


class ConfluenceAttachmentRequest(ConfluenceCall):
    """Скачать вложение страницы и распарсить его."""

    OP: ClassVar[str] = "confluence_attachment"

    page_id: str = Field(min_length=1)
    body_format: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    params: ParseParams


class ConfluenceAttachmentAnswer(BaseModel):
    """Текст вложения целиком."""

    model_config = ConfigDict(extra="forbid")

    text: str
