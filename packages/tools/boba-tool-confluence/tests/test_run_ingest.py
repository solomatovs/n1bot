"""Ручной прогон индексации Confluence: функция вызывается напрямую.

Конфиг прогона берётся из [tool.ingest]; запись идёт в ту же базу знаний, что
у приложения, поэтому цель задаётся в RunArgs осознанно.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.tool.confluence.ingest_tools import (
    IngestToolConfig,
    confluence_index_page,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_ID: ClassVar[str] = "950276"

    ATTACHMENTS: ClassVar[bool] = False

    OCR: ClassVar[bool] = False


@pytest.fixture(scope="module")
def ingest_cfg(raw_config) -> IngestToolConfig:
    return bind(raw_config, path="tool.ingest", model=IngestToolConfig)


async def test_run_confluence_ingest(ingest_cfg: IngestToolConfig) -> None:
    body = ToolMain.toolset(confluence_index_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            page_id=RunArgs.PAGE_ID,
            attachments=RunArgs.ATTACHMENTS,
            ocr=RunArgs.OCR,
            cfg=ingest_cfg,
        )
    ).llm_view()

    print(content)
