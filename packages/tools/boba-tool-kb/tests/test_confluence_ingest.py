"""Integration: `confluence_space_ingest` + `confluence_page_ingest`.

Реальная индексация Confluence-страниц в `kb_chunks` (collection pinned
в `[tool.kb.confluence_ingest].collection`). Проверяем, что pipeline
отрабатывает end-to-end без ошибок (failed=0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.text import StructuralChunker
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.ingest_config import ConfluenceIngestConfig
from boba.tool.kb.confluence.tools.page_ingest import confluence_page_ingest
from boba.tool.kb.confluence.tools.space_ingest import confluence_space_ingest
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

    from boba.provider.openai import OpenAICompatEmbedder

pytestmark = pytest.mark.integration


def test_confluence_page_ingest_real(  # noqa: PLR0913 — integration test
    confluence_ingest_cfg: ConfluenceIngestConfig,
    confluence_cfg: ConfluenceConnectionConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_chunker: StructuralChunker,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_page_ingest` индексирует явный список page_ids в pinned-коллекцию."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    result = confluence_page_ingest(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        chunker=kb_chunker,
        ingest_cfg=confluence_ingest_cfg,
        conn_cfg=confluence_cfg,
        page_ids=test_cfg.confluence_page_ids,
        prune_missing=False,
    )

    assert result["collection"] == confluence_ingest_cfg.collection
    assert result["page_ids"] == test_cfg.confluence_page_ids
    assert result["failed"] == 0, f"some sources failed: {result}"


def test_confluence_space_ingest_real(  # noqa: PLR0913 — integration test
    confluence_ingest_cfg: ConfluenceIngestConfig,
    confluence_cfg: ConfluenceConnectionConfig,
    kb_store: PostgresChunkStore,
    kb_collections_store: PostgresCollectionsStore,
    kb_embedder: OpenAICompatEmbedder,
    kb_chunker: StructuralChunker,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_space_ingest` индексирует все страницы space в pinned-коллекцию."""
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    result = confluence_space_ingest(
        chunk_store=kb_store,
        collections_store=kb_collections_store,
        embedder=kb_embedder,
        chunker=kb_chunker,
        ingest_cfg=confluence_ingest_cfg,
        conn_cfg=confluence_cfg,
        space_keys=[test_cfg.confluence_space_key],
        prune_missing=False,
    )

    assert result["collection"] == confluence_ingest_cfg.collection
    assert result["space_keys"] == [test_cfg.confluence_space_key]
    assert result["failed"] == 0, f"some sources failed: {result}"
