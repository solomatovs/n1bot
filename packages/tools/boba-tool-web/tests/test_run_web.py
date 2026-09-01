"""Ручной прогон web-инструментов: функции вызываются напрямую с явным cfg.

Хост берётся из whitelist'а [tool.web] конфига приложения.
"""

from __future__ import annotations

import pytest

from boba.config import bind
from boba.tool.web.tools import WebGrepConfig, web_fetch_page, web_grep_page
from boba.toolkit.entry import ToolMain
from boba.transport.http.profile import HttpProfile

pytestmark = [pytest.mark.run, pytest.mark.anyio]

CONNECTION = "confluence"


@pytest.fixture(scope="module")
def web_cfg(raw_config) -> WebGrepConfig:
    """Лимиты из [tool.web], whitelist — сервисный [web.confluence] по его хосту."""
    limits = bind(raw_config, path="tool.web", model=WebGrepConfig)
    service = bind(raw_config, path="web.confluence", model=HttpProfile)
    if service.base_url is None:
        raise AssertionError("[web.confluence] has no base_url")

    return limits.model_copy(update={"profiles": {CONNECTION: service}})


@pytest.fixture(scope="module")
def whitelisted_url(web_cfg: WebGrepConfig) -> str:
    return f"https://{web_cfg.profiles[CONNECTION].host()}/"


async def test_run_web_fetch(web_cfg: WebGrepConfig, whitelisted_url: str) -> None:
    body = ToolMain.toolset(web_fetch_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        url=whitelisted_url,
        connection_name=CONNECTION,
        as_markdown=True,
        line_offset=0,
        line_count=20,
        cfg=web_cfg,
    )

    print(content)


async def test_run_web_grep(web_cfg: WebGrepConfig, whitelisted_url: str) -> None:
    body = ToolMain.toolset(web_grep_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        url=whitelisted_url,
        connection_name=CONNECTION,
        pattern=".",
        cfg=web_cfg,
    )

    print(content)
