"""Контракт web-payload'а: запрос в сеть делает песочница."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WebFetchRequest",
    "WebFetchTrailer",
    "WebGrepRequest",
    "WebGrepRow",
    "WebGrepTrailer",
    "WebProfile",
]


class WebProfile(BaseModel):
    """Транспортная часть профиля для payload'а; auth несёт раскрытые креды.

    Секреты едут через stdin (не в argv/логах); httpx.Auth собирается в песочнице.
    """

    model_config = ConfigDict(extra="forbid")

    timeout_sec: float = Field(gt=0)
    ssl_verify: bool
    auth: dict[str, str] = Field(
        description="Метод авторизации и его креды: {'method': 'none'|...}.",
    )


class WebFetchRequest(BaseModel):
    """Скачать страницу и вернуть окно строк."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "web_fetch"

    op: str = Field(min_length=1)
    url: str = Field(min_length=1)
    profile: WebProfile
    as_markdown: bool
    line_offset: int = Field(ge=0)
    line_count: int = Field(ge=1)


class WebFetchTrailer(BaseModel):
    """Итог скачивания: окно ушло кадрами, здесь счётчики для пагинации."""

    model_config = ConfigDict(extra="forbid")

    source_url: str
    total_lines: int = Field(ge=0)
    returned_lines: int = Field(ge=0)


class WebGrepRequest(BaseModel):
    """Скачать страницу и поискать в ней совпадения."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "web_grep"

    op: str = Field(min_length=1)
    url: str = Field(min_length=1)
    profile: WebProfile
    as_markdown: bool
    pattern: str = Field(min_length=1)
    case_insensitive: bool
    context: int = Field(ge=0)
    limit: int = Field(ge=1)
    fixed_string: bool
    max_text_chars: int = Field(ge=1)


class WebGrepRow(BaseModel):
    """Совпадение: номер строки, сама строка и контекст вокруг."""

    model_config = ConfigDict(extra="forbid")

    line: int = Field(ge=1)
    content: str
    before: tuple[str, ...]
    after: tuple[str, ...]


class WebGrepTrailer(BaseModel):
    """Итог поиска: совпадения ушли кадрами-записями."""

    model_config = ConfigDict(extra="forbid")

    source_url: str
