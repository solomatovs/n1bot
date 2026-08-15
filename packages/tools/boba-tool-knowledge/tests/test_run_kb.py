"""Ручной прогон kb-поиска: функции вызываются напрямую с явным cfg.

Подключение и эмбеддер берутся из [tool.kb] конфига приложения.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb.tools import KbToolConfig, kb_fts_search, kb_vector_search
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    QUERY: ClassVar[str] = "данные"

    TOP_K: ClassVar[int] = 5


@pytest.fixture(scope="module")
def kb_cfg(raw_config) -> KbToolConfig:
    return bind(raw_config, path="tool.kb", model=KbToolConfig)


async def test_run_kb_vector_search(kb_cfg: KbToolConfig) -> None:
    body = ToolMain.toolset(kb_vector_search)[0].coroutine
    assert body is not None

    content, _artifact = await body(
        query=RunArgs.QUERY, top_k=RunArgs.TOP_K, cfg=kb_cfg
    )

    print(content)


async def test_run_kb_fts_search(kb_cfg: KbToolConfig) -> None:
    body = ToolMain.toolset(kb_fts_search)[0].coroutine
    assert body is not None

    content, _artifact = await body(
        query=RunArgs.QUERY, top_k=RunArgs.TOP_K, cfg=kb_cfg
    )

    print(content)
