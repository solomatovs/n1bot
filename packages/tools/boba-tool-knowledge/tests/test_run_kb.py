"""Ручной прогон инструментов kb: функции вызываются напрямую с явным cfg.

Подключение, схема и эмбеддер берутся из [tool.kb] конфига приложения.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from omegaconf import OmegaConf

from boba.config import bind
from boba.tool.kb.tools import (
    KbToolConfig,
    kb_catalog2,
    kb_fts_search2,
    kb_node2,
    kb_trgm_search2,
    kb_vector_search2,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    QUERY: ClassVar[str] = "данные"
    SURFACES: ClassVar[list[str]] = []
    ASPECTS: ClassVar[list[str]] = []
    OFFSET: ClassVar[int] = 0
    LIMIT: ClassVar[int] = 5
    NODE_ID: ClassVar[int] = 1


@pytest.fixture(scope="module")
def kb_cfg(raw_config) -> KbToolConfig:
    """Конфиг с процессным запуском: кэш моделей эмбеддинга берётся с хоста."""
    copied = raw_config.copy()
    OmegaConf.update(copied, "env.tool_launcher", "process")

    return bind(copied, path="tool.kb", model=KbToolConfig)


async def test_run_kb_catalog(kb_cfg: KbToolConfig) -> None:
    body = ToolMain.toolset(kb_catalog2)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    print((await body(cfg=kb_cfg)).llm_view())


@pytest.mark.parametrize("search", [kb_fts_search2, kb_trgm_search2, kb_vector_search2])
async def test_run_kb_search(kb_cfg: KbToolConfig, search) -> None:
    body = ToolMain.toolset(search)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            query=RunArgs.QUERY,
            surfaces=RunArgs.SURFACES,
            aspects=RunArgs.ASPECTS,
            offset=RunArgs.OFFSET,
            limit=RunArgs.LIMIT,
            cfg=kb_cfg,
        )
    ).llm_view()

    print(content)


async def test_run_kb_node(kb_cfg: KbToolConfig) -> None:
    body = ToolMain.toolset(kb_node2)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    print((await body(node_id=RunArgs.NODE_ID, aspects=[], cfg=kb_cfg)).llm_view())
