"""Ручной прогон confluence-чтения: функции вызываются напрямую с явным cfg.

Профиль соединения берётся из [tool.confluence] конфига приложения.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb.confluence.tools import (
    ConfluenceToolsConfig,
    confluence_fetch,
    confluence_search,
    confluence_spaces,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_ID: ClassVar[str] = "98314"

    QUERY: ClassVar[str] = "данные"


@pytest.fixture(scope="module")
def confluence_cfg(raw_config) -> ConfluenceToolsConfig:
    return bind(raw_config, path="tool.confluence", model=ConfluenceToolsConfig)


async def test_run_confluence_spaces(confluence_cfg: ConfluenceToolsConfig) -> None:
    body = ToolMain.toolset(confluence_spaces)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(limit=10, cfg=confluence_cfg)

    print(content)


async def test_run_confluence_search(confluence_cfg: ConfluenceToolsConfig) -> None:
    body = ToolMain.toolset(confluence_search)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        query=RunArgs.QUERY, limit=5, offset=0, cfg=confluence_cfg
    )

    print(content)


async def test_run_confluence_fetch(confluence_cfg: ConfluenceToolsConfig) -> None:
    body = ToolMain.toolset(confluence_fetch)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(page_id=RunArgs.PAGE_ID, cfg=confluence_cfg)

    print(content[:500])
