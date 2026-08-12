"""Контракт web-узлов: аргументы модели, запросы payload'а, квитанции и стадии.

Пользовательская часть узла (WebFetchArgs/WebGrepArgs) — то, что задаёт модель;
профиль соединения и лимиты добавляет обогатитель приложения. Креды профиля
живут SecretStr и раскрываются только при json-сериализации запроса в tool_args.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.toolkit.channels import StreamFormat
from boba.toolkit.secrets import SecretDump
from boba.toolkit.workflow import StageContract
from boba.transport.http import HttpProfile
from boba.transport.http.auth import WebAuth

__all__ = [
    "WebFetchArgs",
    "WebFetchRequest",
    "WebFetchTrailer",
    "WebGrepArgs",
    "WebGrepRequest",
    "WebGrepRow",
    "WebGrepTrailer",
    "WebNodes",
    "WebOp",
    "WebProfile",
]


class WebOp(StrEnum):
    """Операции web-payload'а; имена узлов реестра стадий совпадают с ними."""

    FETCH = "web_fetch"
    GREP = "web_grep"


class WebProfile(BaseModel):
    """Транспортная часть профиля для payload'а: таймаут, TLS и метод авторизации.

    tool_args песочницы — доверенный канал: только там креды едут раскрытыми.
    """

    model_config = ConfigDict(extra="forbid")

    timeout_sec: float = Field(gt=0)
    ssl_verify: bool
    auth: WebAuth

    @classmethod
    def of(cls, profile: HttpProfile) -> WebProfile:
        """Транспортный срез профиля соединения; креды остаются SecretStr."""
        return cls(
            timeout_sec=profile.timeout_sec,
            ssl_verify=profile.ssl_verify,
            auth=profile.auth,
        )

    @field_serializer("auth", when_used="json")
    def _dump_auth(self, value: WebAuth) -> dict[str, Any]:
        """Секреты метода авторизации раскрываются только здесь."""
        return SecretDump.of(value)


class WebFetchArgs(BaseModel):
    """Скачать страницу и вернуть окно строк: пользовательская часть узла."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    as_markdown: bool
    line_offset: int = Field(ge=0)
    line_count: int = Field(ge=1)


class WebFetchRequest(WebFetchArgs):
    """Запрос payload'а: аргументы узла плюс профиль соединения."""

    op: Literal[WebOp.FETCH]
    profile: WebProfile


class WebFetchTrailer(BaseModel):
    """Итог скачивания: окно ушло потоком, здесь счётчики для пагинации."""

    model_config = ConfigDict(extra="forbid")

    source_url: str
    total_lines: int = Field(ge=0)
    returned_lines: int = Field(ge=0)


class WebGrepArgs(BaseModel):
    """Поиск по содержимому страницы: пользовательская часть узла."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    as_markdown: bool
    case_insensitive: bool
    context: int = Field(ge=0)
    limit: int = Field(ge=1)
    fixed_string: bool


class WebGrepRequest(WebGrepArgs):
    """Запрос payload'а: аргументы узла, профиль соединения и лимит конфига."""

    op: Literal[WebOp.GREP]
    profile: WebProfile
    max_text_chars: int = Field(ge=1)


class WebGrepRow(BaseModel):
    """Совпадение: номер строки, сама строка и контекст вокруг."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int = Field(ge=1)
    content: str
    before: tuple[str, ...]
    after: tuple[str, ...]


class WebGrepTrailer(BaseModel):
    """Итог поиска: совпадения ушли строками NDJSON."""

    model_config = ConfigDict(extra="forbid")

    source_url: str


class WebNodes:
    """Описание web-узлов для реестра стадий: entry payload'а и контракты потоков.

    Профиль песочницы и обогатитель args добавляет приложение: whitelist хостов
    и лимиты выдачи живут в конфиге инструмента, а не в транспорте.
    """

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.web.payload")

    FETCH: ClassVar[StageContract] = StageContract(
        out=StreamFormat.TEXT,
        result=WebFetchTrailer,
    )

    GREP: ClassVar[StageContract] = StageContract(
        out=StreamFormat.NDJSON,
        result=WebGrepTrailer,
    )
