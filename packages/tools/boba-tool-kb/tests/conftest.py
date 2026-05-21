"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

ВСЕ тесты — integration: ходят в реальный postgres, реальный embeddings
endpoint, реальный Confluence. Параметры конфигурации читаются через
систему конфигурирования (BobaFlatSettings) из:

- `[tool.kb]`              → `KbConfig` (collection, files_folder, embedder,
                              search params, chunker).
- `[tool.kb.postgres]`     → `PostgresConnectionConfig` (host/port/user/...).
- `[tool.kb.confluence]`   → `ConfluenceConnectionConfig` (base_url/auth/...).
- `[tool.kb.fts]`          → `FtsConfig` (одна whitelist-таблица).
- `[test.kb]`              → `KbIntegrationTestConfig` (тестовые параметры:
                              page_ids, search-query, space_key, ...).

Каждая конфиг-фикстура skip-ает тест с понятной причиной, если секция
не сконструировалась (validation failed) — оператор не заполнил секцию.

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from openai import OpenAI
from pgvector.psycopg import register_vector
from pydantic import Field, ValidationError

from boba.db.postgres import PostgresPool
from boba.html import HtmlReader
from boba.indexing import (
    ChunkerId,
    DispatchReader,
    FixedDigestPrefix,
    PipelineContext,
    PipelineId,
    ReaderId,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.kbdoc import KbDocReader
from boba.markdown import MarkdownReader
from boba.provider.openai import OpenAICompatEmbedder
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.kb.config import KbConfig
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.db import PgFtsKnowledgeBase
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.migrations import apply_bootstrap
from boba.tool.kb.postgres_config import PostgresConnectionConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.transport.fs import FsKeys
from boba.transport.http import HttpTransport
from boba.workspace.contract import WorkspaceId

# --------------------------------------------------------------------------- #
# KbIntegrationTestConfig — тестовая секция [test.kb]
# --------------------------------------------------------------------------- #


class KbIntegrationTestConfig(BobaFlatSettings):
    """Параметры integration-тестов KB-плагина (секция `[test.kb]`)."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="test.kb",
    )

    kb_search_query: str = Field(
        default="",
        description="Search-запрос для test_kb_search. Пусто = skip.",
    )
    kb_search_top_k: int = Field(
        default=5,
        ge=1,
        description="top_k для test_kb_search.",
    )

    confluence_page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Реальные page_id для test_confluence_page_download / page_ingest. "
            "Пусто = тесты скипаются."
        ),
    )
    confluence_space_key: str = Field(
        default="",
        description=(
            "Реальный space_key для test_confluence_space_download / "
            "space_ingest / discovery. Пусто = skip."
        ),
    )
    confluence_cql: str = Field(
        default="",
        description=(
            "Реальный CQL для test_cql_source_returns_pages_for_real_cql. "
            "Пусто = skip."
        ),
    )

    fts_query: str = Field(
        default="",
        description="Запрос для тестов FTS (`fts_search`). Пусто = skip.",
    )


# --------------------------------------------------------------------------- #
# Config-фикстуры (skip-if-not-configured)
# --------------------------------------------------------------------------- #


@pytest.fixture
def kb_cfg() -> KbConfig:
    """`KbConfig` из `[tool.kb]`; skip при ошибке валидации."""
    try:
        return KbConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb] не сконфигурирован: {e}")


@pytest.fixture
def pg_cfg() -> PostgresConnectionConfig:
    """`PostgresConnectionConfig` из `[tool.kb.postgres]`; skip при ошибке."""
    try:
        return PostgresConnectionConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.postgres] не сконфигурирован: {e}")


@pytest.fixture
def confluence_cfg() -> ConfluenceConnectionConfig:
    """`ConfluenceConnectionConfig` из `[tool.kb.confluence]`; skip при ошибке."""
    try:
        return ConfluenceConnectionConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence] не сконфигурирован: {e}")


@pytest.fixture
def fts_cfg() -> FtsConfig:
    """`FtsConfig` из `[tool.kb.fts]`; skip при ошибке."""
    try:
        return FtsConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.fts] не сконфигурирован: {e}")


@pytest.fixture
def test_cfg() -> KbIntegrationTestConfig:
    """`KbIntegrationTestConfig` из `[test.kb]`; skip при ошибке валидации."""
    try:
        return KbIntegrationTestConfig()
    except ValidationError as e:
        pytest.skip(f"[test.kb] не сконфигурирован: {e}")


# --------------------------------------------------------------------------- #
# Реальные backend-фикстуры
# --------------------------------------------------------------------------- #


@pytest.fixture
def kb_pool(pg_cfg: PostgresConnectionConfig) -> PostgresPool:
    """`PostgresPool` из `[tool.kb.postgres]` с register_vector + bootstrap.

    Singleton-cached `PostgresPool.get(...)` — повторный вызов с тем же
    DSN+pool-sizes возвращает тот же инстанс. Close НЕ зовём здесь
    (cache живёт до process exit; повторное использование между тестами).
    """
    pool = PostgresPool.get(
        pg_cfg.to_pool_config(),
        configure=register_vector,
    )
    with pool.connection() as conn:
        apply_bootstrap(conn)
    return pool


@pytest.fixture
def kb_embedder(kb_cfg: KbConfig) -> OpenAICompatEmbedder:
    """OpenAI-совместимый embedder для kb_search/files_ingest."""
    client = OpenAI(
        base_url=kb_cfg.embedding_base_url or None,
        api_key=kb_cfg.embedding_api_key or "unused",
    )
    return OpenAICompatEmbedder(client=client, model=kb_cfg.embedding_model)


@pytest.fixture
def kb_store(
    kb_pool: PostgresPool,
    kb_embedder: OpenAICompatEmbedder,
) -> PostgresVectorStore:
    """Write-side store для ingest-тулов."""
    return PostgresVectorStore(
        pool=kb_pool,
        embedder=kb_embedder,
        embedding_dim=kb_embedder.dim(),
    )


@pytest.fixture
def kb_knowledge_base(
    kb_cfg: KbConfig,
    kb_pool: PostgresPool,
    kb_embedder: OpenAICompatEmbedder,
) -> PostgresKnowledgeBase:
    """Read-side KB для kb_search (hybrid RRF)."""
    return PostgresKnowledgeBase(
        pool=kb_pool,
        embedder=kb_embedder,
        embedding_dim=kb_embedder.dim(),
        snippet_chars=kb_cfg.snippet_chars,
        fts_language=kb_cfg.fts_language,
        rrf_k=kb_cfg.rrf_k,
        rrf_pool=kb_cfg.rrf_pool,
    )


@pytest.fixture
def kb_dispatch_reader() -> DispatchReader[str]:
    """DispatchReader для files_ingest: md → KbDocReader, html/htm → HtmlReader."""
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": KbDocReader(inner=MarkdownReader()),
            "html": HtmlReader(),
            "htm": HtmlReader(),
        },
        reader_id=ReaderId("kb-dispatch"),
    )


@pytest.fixture
def kb_chunker(kb_cfg: KbConfig) -> StructuralChunker:
    """StructuralChunker с chunk_size/overlap из `[tool.kb]`."""

    def splitter_factory(extra_overhead: int) -> OverlapCharSplitter:
        return OverlapCharSplitter(
            chunk_size=kb_cfg.chunk_size,
            chunk_overlap=kb_cfg.chunk_overlap,
            extra_overhead=extra_overhead,
        )

    factory: SplitterFactory = splitter_factory
    return StructuralChunker(
        chunker_id=ChunkerId("kb-structural"),
        splitter_factory=factory,
        id_strategy=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=16),
        ),
    )


@pytest.fixture
def pg_fts_kb(
    fts_cfg: FtsConfig,
    kb_pool: PostgresPool,
) -> PgFtsKnowledgeBase:
    """`PgFtsKnowledgeBase` поверх kb_pool (DSN-fallback на `[tool.kb.postgres]`)."""
    return PgFtsKnowledgeBase(
        pool=kb_pool,
        index=fts_cfg.index,
        snippet_options=fts_cfg.snippet_options,
    )


@pytest.fixture
def confluence_auth(confluence_cfg: ConfluenceConnectionConfig) -> httpx.Auth | None:
    """`httpx.Auth | None` (PatAuth | BasicAuth | None для anonymous)."""
    return ConfluenceConnection.make_auth(confluence_cfg)


@pytest.fixture
def confluence_transport(
    confluence_cfg: ConfluenceConnectionConfig,
) -> HttpTransport:
    """Real HttpTransport с timeout/ssl_verify из `[tool.kb.confluence]`."""
    return ConfluenceConnection.make_transport(confluence_cfg)


@pytest.fixture
def workspace_shell(tmp_path: Path):
    """Real `FsProjectWorkspaceShell` на pytest tmp_path.

    Импорт `boba.agent.workspace_fs` — внутри фикстуры, чтобы не тащить
    `boba-agent` как обязательную тест-dep при выключенных фикстурах.
    """
    from boba.agent.workspace_fs.shell import FsProjectWorkspaceShell

    return FsProjectWorkspaceShell(
        workspace_id=WorkspaceId("test"),
        root=tmp_path,
    )


# --------------------------------------------------------------------------- #
# Pipeline helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def pipeline_ctx() -> PipelineContext:
    """`PipelineContext` с тестовым `PipelineId('t')`."""
    return PipelineContext(pipeline_id=PipelineId("t"))
