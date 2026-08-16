"""Web-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.web.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
WebRequestError — страница не скачалась (сеть, TLS, HTTP-статус).
UnknownHostError — хост URL вне whitelist'а конфига.
ResultTooLargeError — содержимое превысило max_result_chars конфига.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final

import httpx
from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from boba.text.grep import GrepLimits, TextGrep
from boba.tool.web.connection import UnknownHostError, WebConnection
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import (
    JsonResult,
    ResultTooLargeError,
    TableResult,
    ToolResult,
    pack_result,
)
from boba.toolkit.types import SecretRevealing
from boba.transport.http import HttpProfile


class WebRequestError(Exception):
    """Страница не скачалась; текст готов для пользователя."""


class WebErrorKind(StrEnum):
    """Ожидаемые отказы web-инструментов."""

    REQUEST_FAILED = "web_request_failed"
    UNKNOWN_HOST = "unknown_host"
    RESULT_TOO_LARGE = "result_too_large"


class WebGrepConfig(SecretRevealing, WebConnection):
    """Конфиг web-инструментов: whitelist хостов и лимиты выдачи; [tool.web]."""

    SECTION: ClassVar[str] = "tool.web"

    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма результата (символов).",
    )


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
                auth=profile.auth.httpx_auth(),
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


@tool(response_format="content_and_artifact")
async def web_fetch_page(
    url: Annotated[str, Field(min_length=1, description="URL для скачивания")],
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
    cfg: Annotated[WebGrepConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Скачивает URL и возвращает окно строк; total_lines — для пагинации."""
    profile = cfg.resolve_profile(url)

    page = await WebPage.load(
        url, profile, as_markdown=as_markdown, max_chars=cfg.max_result_chars
    )

    lines = page.splitlines()
    window = lines[line_offset : line_offset + line_count]

    payload = {
        "content": "\n".join(window),
        "source_url": url,
        "total_lines": len(lines),
        "returned_lines": len(window),
    }

    artifact = JsonResult(payload=payload)
    return pack_result(artifact)


@tool(response_format="content_and_artifact")
async def web_grep_page(  # noqa: PLR0913
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания."),
    ],
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
    cfg: Annotated[WebGrepConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Найти совпадения pattern в содержимом страницы."""
    profile = cfg.resolve_profile(url)

    text = await WebPage.load(
        url, profile, as_markdown=as_markdown, max_chars=cfg.max_result_chars
    )

    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=fixed_string, case_insensitive=case_insensitive
    )

    limits = GrepLimits(context=context, limit=limit, clip_chars=cfg.max_text_chars)
    rows, note = TextGrep.matched_rows(text, compiled, limits, f"url={url}")

    table = TableResult(rows=rows, note=note, metadata={"url": url})
    return pack_result(table)


EXPECTED: Mapping[type[Exception], WebErrorKind] = {
    WebRequestError: WebErrorKind.REQUEST_FAILED,
    UnknownHostError: WebErrorKind.UNKNOWN_HOST,
    ResultTooLargeError: WebErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(web_fetch_page, web_grep_page)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
