"""Контракт web-payload'а: запрос в сеть делает песочница.

Хост оставляет за собой whitelist хостов: до payload'а доходит только URL,
профиль которого нашёлся в [tool.web] profiles.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WebFetchAnswer",
    "WebFetchRequest",
    "WebGrepAnswer",
    "WebGrepRequest",
    "WebGrepRow",
    "WebProfile",
]


class WebProfile(BaseModel):
    """Транспортная часть профиля, которая нужна payload'у.

    auth несёт раскрытые креды: httpx.Auth собирается уже внутри песочницы,
    потому что digest — это challenge-response, а не готовый заголовок.
    Секреты уходят через stdin, поэтому не видны ни в argv, ни в логах.
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


class WebFetchAnswer(BaseModel):
    """Окно строк и сколько их всего в материализованном контенте."""

    model_config = ConfigDict(extra="forbid")

    content: str
    source_url: str
    total_lines: int = Field(ge=0)
    returned_lines: int = Field(ge=0)

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump()


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


class WebGrepAnswer(BaseModel):
    """Найденные совпадения по порядку в тексте."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[WebGrepRow, ...]
    source_url: str
