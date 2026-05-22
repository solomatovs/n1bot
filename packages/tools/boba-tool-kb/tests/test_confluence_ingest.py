"""Integration: `confluence_*_ingest` tools."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.tools.page_ingest import (
    ConfluencePageIngestConfig,
    confluence_page_ingest,
)
from boba.tool.kb.confluence.tools.space_ingest import (
    ConfluenceSpaceIngestConfig,
    confluence_space_ingest,
)

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_page_ingest_real(
    confluence_page_ingest_cfg: ConfluencePageIngestConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_page_ingest` индексирует явный список page_ids."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    result = confluence_page_ingest(
        cfg=confluence_page_ingest_cfg,
        page_ids=test_cfg.confluence_page_ids,
        prune_missing=False,
    )

    assert result["collection"] == confluence_page_ingest_cfg.collection
    assert result["page_ids"] == test_cfg.confluence_page_ids
    assert result["failed"] == 0, f"some sources failed: {result}"


def test_confluence_space_ingest_real(
    confluence_space_ingest_cfg: ConfluenceSpaceIngestConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_space_ingest` индексирует все страницы space."""
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    result = confluence_space_ingest(
        cfg=confluence_space_ingest_cfg,
        space_keys=[test_cfg.confluence_space_key],
        prune_missing=False,
    )

    assert result["collection"] == confluence_space_ingest_cfg.collection
    assert result["space_keys"] == [test_cfg.confluence_space_key]
    assert result["failed"] == 0, f"some sources failed: {result}"
