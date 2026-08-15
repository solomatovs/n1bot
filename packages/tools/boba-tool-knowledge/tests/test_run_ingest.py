"""Ручной прогон индексации Confluence: функция вызывается напрямую.

Конфиг прогона берётся из [tool.ingest]; запись идёт в ту же базу знаний, что
у приложения, поэтому цель задаётся в RunArgs осознанно.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb.confluence.ingest_tools import (
    IngestToolConfig,
    confluence_index_pages,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_IDS: ClassVar[list[str]] = ["950276"]

    PRUNE_MISSING: ClassVar[bool] = False

    FORCE_UPDATE: ClassVar[bool] = False


@pytest.fixture(scope="module")
def ingest_cfg(raw_config) -> IngestToolConfig:
    return bind(raw_config, path="tool.ingest", model=IngestToolConfig)


async def test_run_confluence_ingest(ingest_cfg: IngestToolConfig) -> None:
    body = ToolMain.toolset(confluence_index_pages)[0].coroutine
    assert body is not None

    content, _artifact = await body(
        page_ids=RunArgs.PAGE_IDS,
        prune_missing=RunArgs.PRUNE_MISSING,
        force_update=RunArgs.FORCE_UPDATE,
        cfg=ingest_cfg,
    )

    print(content)
