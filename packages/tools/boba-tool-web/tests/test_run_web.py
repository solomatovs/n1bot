"""Ручной прогон web-инструментов: функции вызываются напрямую с явным cfg.

Хост берётся из whitelist'а [tool.web] конфига приложения.
"""

from __future__ import annotations

import pytest

from boba.settings import bind
from boba.tool.web.tools import WebGrepConfig, web_fetch_page, web_grep_page
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


@pytest.fixture(scope="module")
def web_cfg(raw_config) -> WebGrepConfig:
    return bind(raw_config, path="tool.web", model=WebGrepConfig)


@pytest.fixture(scope="module")
def whitelisted_url(web_cfg: WebGrepConfig) -> str:
    hosts = sorted(web_cfg.profiles)
    if not (hosts):
        raise AssertionError(
            "[tool.web.profiles] пуст — web-инструментам некуда ходить"
        )
    return f"https://{hosts[0]}/"


async def test_run_web_fetch(web_cfg: WebGrepConfig, whitelisted_url: str) -> None:
    body = ToolMain.toolset(web_fetch_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        url=whitelisted_url,
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
        pattern=".",
        cfg=web_cfg,
    )

    print(content)
