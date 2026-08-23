"""Web-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.web.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
WebRequestError — страница не скачалась (сеть, TLS, HTTP-статус).
UnknownConnectionError — имя соединения вне whitelist'а.
UnknownHostError — хост URL не покрыт выбранным соединением.
ResultTooLargeError — содержимое превысило max_result_chars конфига.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Final

import httpx
from pydantic import Field

from boba.text.grep import GrepLimits, TextGrep
from boba.tool.web.connection import UnknownHostError, WebConnection
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    ResultTooLargeError,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.toolkit.sql import ConnectionName, UnknownConnectionError
from boba.toolkit.types import SecretRevealing
from boba.transport.http import HttpProfile


class WebRequestError(Exception):
    """Страница не скачалась; текст готов для пользователя."""


class WebErrorKind(StrEnum):
    """Ожидаемые отказы web-инструментов."""

    REQUEST_FAILED = "web_request_failed"
    UNKNOWN_TARGET = "unknown_target"
    UNKNOWN_HOST = "unknown_host"
    RESULT_TOO_LARGE = "result_too_large"


class WebGrepConfig(SecretRevealing, WebConnection):
    """Конфиг web-инструментов: whitelist соединений и лимиты выдачи; [tool.web]."""

    SECTION: ClassVar[str] = "tool.web"

    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины строки grep-выдачи: совпадения и контекста.",
    )
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма результата (символов).",
    )


class PageFormat(StrEnum):
    """Формат содержимого страницы; он же язык markdown-блока показа."""

    MARKDOWN = "markdown"
    HTML = "html"

    @classmethod
    def of(cls, *, as_markdown: bool) -> PageFormat:
        if as_markdown:
            return cls.MARKDOWN

        return cls.HTML


@dataclass(frozen=True)
class PageWindow:
    """Окно строк страницы: показанный кусок и его место в документе."""

    url: str
    offset: int
    lines: Sequence[str]
    total: int

    @classmethod
    def of(cls, url: str, page: str, offset: int, count: int) -> PageWindow:
        lines = page.splitlines()
        window = lines[offset : offset + count]

        return cls(url=url, offset=offset, lines=window, total=len(lines))

    def text(self) -> str:
        return "\n".join(self.lines)

    def note(self) -> str:
        """Сводка окна: источник и место среза в документе."""
        if not self.lines:
            return f"url={self.url}; no lines at offset {self.offset} of {self.total}"

        first = self.offset + 1
        last = self.offset + len(self.lines)

        return f"url={self.url}; lines {first}-{last} of {self.total}"


class WebPage:
    """Скачивание страницы профилем хоста и конверсия в markdown."""

    ENCODING: ClassVar[str] = "utf-8"
    HEADING_STYLE: ClassVar[str] = "ATX"

    @classmethod
    async def load(
        cls,
        url: str,
        profile: HttpProfile,
        *,
        as_markdown: bool,
        max_chars: int,
    ) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=profile.timeout_sec,
                verify=profile.ssl_verify,
                follow_redirects=True,
                auth=profile.httpx_auth(),
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                body = await response.aread()
        except httpx.HTTPError as exc:
            msg = f"web request failed: {type(exc).__name__}: {exc}"
            raise WebRequestError(msg) from exc

        text = body.decode(cls.ENCODING, errors="replace")

        if as_markdown:
            import markdownify  # noqa: PLC0415 — тяжёлый, нужен не каждому вызову

            text = markdownify.markdownify(text, heading_style=cls.HEADING_STYLE)

        if len(text) > max_chars:
            raise ResultTooLargeError.chars_limit(max_chars)

        return text


@tool
async def web_connection_list(
    cfg: Annotated[WebGrepConfig, Injected],
) -> tuple[str, ToolResult]:
    """Доступные соединения web-инструментов: connection_name и хост, который
    оно покрывает (точный либо шаблон *.domain). Выбери connection_name,
    чей хост покрывает URL запроса."""
    return pack_result(cfg.targets_table())


@tool
async def web_fetch_page(  # noqa: PLR0913
    url: Annotated[str, Field(min_length=1, description="URL для скачивания")],
    connection_name: ConnectionName,
    as_markdown: Annotated[
        bool,
        Field(description="true — конвертирует HTML->Markdown"),
    ],
    line_offset: Annotated[
        int,
        Field(ge=0, description="Вернуть контент начиная со строки"),
    ],
    line_count: Annotated[
        int,
        Field(ge=1, description="Сколько строк вернуть начиная с line_offset"),
    ],
    cfg: Annotated[WebGrepConfig, Injected],
) -> tuple[str, ToolResult]:
    """Скачивает URL соединением connection_name (см. web_connection_list) и
    возвращает окно строк; строка под текстом называет срез и общее число
    строк — по ней листай страницу дальше."""
    profile = cfg.resolve_for(connection_name, url)

    page = await WebPage.load(
        url, profile, as_markdown=as_markdown, max_chars=cfg.max_result_chars
    )

    window = PageWindow.of(url, page, line_offset, line_count)

    artifact = TextResult(
        text=window.text(),
        language=PageFormat.of(as_markdown=as_markdown),
        note=window.note(),
        metadata={"url": url},
    )
    return pack_result(artifact)


@tool
async def web_grep_page(  # noqa: PLR0913
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания."),
    ],
    connection_name: ConnectionName,
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[
        bool,
        Field(description="true — искать по HTML→Markdown-конверсии, иначе по HTML."),
    ] = True,
    case_insensitive: Annotated[
        bool,
        Field(description="Игнорировать регистр. По умолчанию false."),
    ] = False,
    context: Annotated[
        int,
        Field(ge=0, description="Строк контекста до и после каждого совпадения."),
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум совпадений в ответе. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
    *,
    cfg: Annotated[WebGrepConfig, Injected],
) -> tuple[str, ToolResult]:
    """Найти совпадения pattern в содержимом страницы, скачанной соединением
    connection_name (см. web_connection_list)."""
    profile = cfg.resolve_for(connection_name, url)

    text = await WebPage.load(
        url, profile, as_markdown=as_markdown, max_chars=cfg.max_result_chars
    )

    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=fixed_string, case_insensitive=case_insensitive
    )

    limits = GrepLimits(context=context, limit=limit, clip_chars=cfg.max_text_chars)
    report = TextGrep.report(text, compiled, limits, f"url={url}")

    artifact = TextResult(
        text=report.render(),
        language=report.LANG,
        note=report.note,
        metadata={"url": url},
    )
    return pack_result(artifact)


EXPECTED: Mapping[type[Exception], WebErrorKind] = {
    WebRequestError: WebErrorKind.REQUEST_FAILED,
    UnknownConnectionError: WebErrorKind.UNKNOWN_TARGET,
    UnknownHostError: WebErrorKind.UNKNOWN_HOST,
    ResultTooLargeError: WebErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(web_connection_list, web_fetch_page, web_grep_page)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
