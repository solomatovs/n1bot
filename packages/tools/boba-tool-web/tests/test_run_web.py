"""Ручной прогон web-инструментов: функции вызываются напрямую.

Профиль соединения в бою подаёт хост; здесь он берётся из
[tool.ingest.confluence] конфига приложения и передаётся параметром.
"""

from __future__ import annotations

import pytest

from boba.config import bind
from boba.tool.web.tools import WebGrepConfig, web_fetch_page, web_grep_page
from boba.toolkit.entry import ToolMain
from boba.transport.http.profile import HttpConnection

pytestmark = [pytest.mark.run, pytest.mark.anyio]

@pytest.fixture(scope="module")
def web_cfg(raw_config) -> WebGrepConfig:
    """Лимиты выдачи из [tool.web]."""
    return bind(raw_config, path="tool.web", model=WebGrepConfig)


@pytest.fixture(scope="module")
def connection(raw_config) -> HttpConnection:
    """Профиль соединения: в бою его подаёт хост из строк пользователя."""
    profile = bind(raw_config, path="tool.ingest.confluence", model=HttpConnection)
    if profile.base_url is None:
        raise AssertionError("[tool.ingest.confluence] has no base_url")

    return profile


@pytest.fixture(scope="module")
def covered_url(connection: HttpConnection) -> str:
    return f"https://{connection.host()}/"


async def test_run_web_fetch(
    web_cfg: WebGrepConfig, connection: HttpConnection, covered_url: str
) -> None:
    body = ToolMain.toolset(web_fetch_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        url=covered_url,
        connection=connection,
        as_markdown=True,
        line_offset=0,
        line_count=20,
        cfg=web_cfg,
    )

    print(content)


async def test_run_web_grep(
    web_cfg: WebGrepConfig, connection: HttpConnection, covered_url: str
) -> None:
    body = ToolMain.toolset(web_grep_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        url=covered_url,
        connection=connection,
        pattern=".",
        cfg=web_cfg,
    )

    print(content)
