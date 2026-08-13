"""Ручной прогон web-операций: WebOps вызывается напрямую.

Профиль соединения берётся из [tool.web.profiles] по хосту URL, аргументы
вызова задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.web import WebGrepConfig
from boba.web.caller import WebCaller
from boba.web.payload import WebOps
from boba.web.protocol import WebFetchRequest, WebGrepRequest

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    HOST: ClassVar[str] = "confl.loshara.com"

    URL: ClassVar[str] = "https://confl.loshara.com/"

    PATTERN: ClassVar[str] = "confluence"

    AS_MARKDOWN: ClassVar[bool] = True

    LINE_OFFSET: ClassVar[int] = 0

    LINE_COUNT: ClassVar[int] = 40

    CASE_INSENSITIVE: ClassVar[bool] = True

    CONTEXT: ClassVar[int] = 2

    LIMIT: ClassVar[int] = 20

    FIXED_STRING: ClassVar[bool] = True

    MAX_TEXT_CHARS: ClassVar[int] = 2000


@pytest.fixture(scope="module")
def web_profile(raw_config):
    cfg = bind(raw_config, path="tool.web", model=WebGrepConfig)

    return WebCaller.transport_of(cfg.profiles[RunArgs.HOST])


async def test_run_web_fetch(web_profile, payload, chunks) -> None:
    request = WebFetchRequest(
        op=WebFetchRequest.OP,
        url=RunArgs.URL,
        profile=web_profile,
        as_markdown=RunArgs.AS_MARKDOWN,
        line_offset=RunArgs.LINE_OFFSET,
        line_count=RunArgs.LINE_COUNT,
    )

    trailer = await WebOps.web_fetch(payload.of(request), chunks.write)

    print(chunks.text())
    print(trailer)


async def test_run_web_grep(web_profile, payload, chunks) -> None:
    request = WebGrepRequest(
        op=WebGrepRequest.OP,
        url=RunArgs.URL,
        profile=web_profile,
        as_markdown=RunArgs.AS_MARKDOWN,
        pattern=RunArgs.PATTERN,
        case_insensitive=RunArgs.CASE_INSENSITIVE,
        context=RunArgs.CONTEXT,
        limit=RunArgs.LIMIT,
        fixed_string=RunArgs.FIXED_STRING,
        max_text_chars=RunArgs.MAX_TEXT_CHARS,
    )

    trailer = await WebOps.web_grep(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)
