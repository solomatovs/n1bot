"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

ВСЕ тесты — integration: ходят в реальный postgres, реальный embeddings
endpoint, реальный Confluence. Параметры конфигурации читаются через
систему конфигурирования (BobaFlatSettings) из:

- `[tool.kb]`              → `KbConfig` (collection, files_folder, search
                              params, chunker).
- `[tool.kb.postgres]`     → `PostgresConnectionConfig` (host/port/user/...).
- `[tool.kb.confluence]`   → `ConfluenceConnectionConfig` (base_url/auth/...).
- `[tool.kb.embedding]`    → `EmbeddingConfig` (model/base_url/api_key).
- `[tool.kb.fts]`          → `FtsConfig` (одна whitelist-таблица).
- `[test.kb]`              → `KbIntegrationTestConfig` (тестовые параметры:
                              page_ids, search-query, space_key, ...).

Каждая конфиг-фикстура skip-ает тест с понятной причиной, если секция
не сконструировалась (validation failed) — оператор не заполнил секцию.

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""

from __future__ import annotations

from dataclasses import replace
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
    RawDocument,
    ReaderId,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.kbdoc import KbDocReader
from boba.provider.openai import OpenAICompatEmbedder
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.core.chunk_store import PostgresChunkStore
from boba.tool.kb.core.collections_store import PostgresCollectionsStore
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.embedding_config import EmbeddingConfig
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tool.kb.core.migrations import apply_bootstrap, ensure_vector_index
from boba.tool.kb.core.postgres_config import PostgresConnectionConfig
from boba.tool.kb.core.vector_store_config import VectorStoreSchemaConfig
from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.db import PgFtsKnowledgeBase
from boba.tool.kb.sql.config import SqlConfig
from boba.tool.kb.sql.executor import SqlExecutor
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
            "Реальный CQL для test_cql_source_returns_pages_for_real_cql. Пусто = skip."
        ),
    )
    confluence_search_query: str = Field(
        default="",
        description=(
            "Plain-text запрос для test_confluence_search "
            '(оборачивается в CQL `text ~ "..."` внутри tool\'а). Пусто = skip.'
        ),
    )
    confluence_search_space: str = Field(
        default="",
        description=(
            "Опциональное `space=` ограничение для test_confluence_search "
            "(чтобы ограничить выдачу одним space-ом). Пусто = без ограничения."
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
def vector_store_cfg() -> VectorStoreSchemaConfig:
    """`VectorStoreSchemaConfig` из `[tool.kb.vector_store]`; skip при ошибке.

    Тот же конфиг получает и bootstrap (`apply_bootstrap` + `ensure_vector_index`
    в фикстурах ниже), и runtime-сторона (`PostgresChunkStore` /
    `PostgresKnowledgeBase`). Тестовая обвязка mimic'ирует операторский
    bootstrap-CLI: одна и та же `schema/chunks_table/collections_table`
    проходит сквозным маршрутом.
    """
    try:
        return VectorStoreSchemaConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.vector_store] не сконфигурирован: {e}")


@pytest.fixture
def confluence_cfg() -> ConfluenceConnectionConfig:
    """`ConfluenceConnectionConfig` из `[tool.kb.confluence]`; skip при ошибке."""
    try:
        return ConfluenceConnectionConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence] не сконфигурирован: {e}")


@pytest.fixture
def embedding_cfg() -> EmbeddingConfig:
    """`EmbeddingConfig` из `[tool.kb.embedding]`; skip при ошибке."""
    try:
        return EmbeddingConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.embedding] не сконфигурирован: {e}")


@pytest.fixture
def fts_cfg() -> FtsConfig:
    """`FtsConfig` из `[tool.kb.fts]`; skip при ошибке.

    `index` — required, грузится из TOML через pydantic-settings; pyright
    не видит config-source loading и считает аргумент обязательным.
    """
    try:
        return FtsConfig()  # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        pytest.skip(f"[tool.kb.fts] не сконфигурирован: {e}")


@pytest.fixture
def sql_cfg() -> SqlConfig:
    """`SqlConfig` из `[tool.kb.sql]`; skip при ошибке."""
    try:
        return SqlConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.sql] не сконфигурирован: {e}")


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
def kb_pool(
    pg_cfg: PostgresConnectionConfig,
    vector_store_cfg: VectorStoreSchemaConfig,
) -> PostgresPool:
    """`PostgresPool` из `[tool.kb.postgres]` с register_vector + bootstrap.

    Singleton-cached `PostgresPool.get(...)` — повторный вызов с тем же
    DSN+pool-sizes возвращает тот же инстанс. Close НЕ зовём здесь
    (cache живёт до process exit; повторное использование между тестами).

    Bootstrap-миграция (`apply_bootstrap`) применяется с теми же
    `schema/chunks_table/collections_table`, что и `PostgresChunkStore`
    в фикстуре `kb_store` ниже — единый `vector_store_cfg`.
    """
    pool = PostgresPool.get(
        pg_cfg.to_pool_config(),
        configure=register_vector,
    )
    with pool.connection() as conn:
        apply_bootstrap(conn, schema_cfg=vector_store_cfg)
    return pool


@pytest.fixture
def kb_embedder(embedding_cfg: EmbeddingConfig) -> OpenAICompatEmbedder:
    """OpenAI-совместимый embedder для kb_search/files_ingest."""
    client = OpenAI(
        base_url=embedding_cfg.base_url or None,
        api_key=embedding_cfg.api_key or "unused",
    )
    return OpenAICompatEmbedder(client=client, model=embedding_cfg.model)


@pytest.fixture
def kb_store(
    kb_pool: PostgresPool,
    kb_embedder: OpenAICompatEmbedder,
    vector_store_cfg: VectorStoreSchemaConfig,
) -> PostgresChunkStore:
    """Write-side store для ingest-тулов.

    Тесты mimic'ируют операторский bootstrap-шаг (CLI
    `boba.tool.kb.core.cli.bootstrap`): `apply_bootstrap` уже отработал
    в `kb_pool`, здесь добавляется HNSW-индекс под текущий embedder.dim().
    В runtime store-у схема считается данностью; в тестах мы её строим
    сами, чтобы fixture-граф не зависел от внешнего CLI-шага. Один и
    тот же `vector_store_cfg` идёт в bootstrap (`kb_pool`), в
    `ensure_vector_index` и в сам store — иначе fixture-граф разъедется.
    """
    with kb_pool.connection() as conn:
        ensure_vector_index(
            conn,
            dim=kb_embedder.dim(),
            schema_cfg=vector_store_cfg,
        )
    return PostgresChunkStore(
        pool=kb_pool,
        embedding_dim=kb_embedder.dim(),
        schema_cfg=vector_store_cfg,
    )


@pytest.fixture
def kb_collections_store(
    kb_pool: PostgresPool,
    vector_store_cfg: VectorStoreSchemaConfig,
) -> PostgresCollectionsStore:
    """Collections-CRUD store для ingest-тулов (`ensure_collection`).

    Тот же `kb_pool` и `vector_store_cfg`, что у `kb_store` — обе фикстуры
    смотрят в одну и ту же схему/таблицы.
    """
    return PostgresCollectionsStore(
        pool=kb_pool,
        schema_cfg=vector_store_cfg,
    )


@pytest.fixture
def kb_knowledge_base(
    kb_cfg: KbConfig,
    kb_pool: PostgresPool,
    kb_embedder: OpenAICompatEmbedder,
    vector_store_cfg: VectorStoreSchemaConfig,
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
        schema_cfg=vector_store_cfg,
    )


@pytest.fixture
def kb_dispatch_reader() -> DispatchReader[str]:
    """DispatchReader для files_ingest: md → KbDocReader, html/htm → HtmlReader."""
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": KbDocReader(),
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
        chunk_id_generator=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=16),
        ),
        content_hasher=Sha256TextEncoder(),
    )


@pytest.fixture
def sql_executor(sql_cfg: SqlConfig) -> SqlExecutor:
    """`SqlExecutor` поверх своего `PostgresPool` (DSN из `[tool.kb.sql]`).

    Read-only + `statement_timeout` зашиты в DSN через libpq
    `options=`-параметр (см. `SqlConfig.session_options`). Close НЕ зовём
    — singleton-cache `PostgresPool.get` живёт до process exit.
    """
    pool = PostgresPool.get(sql_cfg.to_pool_config())
    return SqlExecutor(
        pool=pool,
        max_rows=sql_cfg.max_rows,
        max_cell_chars=sql_cfg.max_cell_chars,
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
def confluence_raw_doc(
    confluence_cfg: ConfluenceConnectionConfig,
    confluence_auth: httpx.Auth | None,
    confluence_transport: HttpTransport,
    test_cfg,
    pipeline_ctx: PipelineContext,
):
    """Реальный `RawDocument` с одной страницы Confluence (до декодинга).

    Берёт первый `page_id` из `[test.kb].confluence_page_ids` (по
    умолчанию cwiki.apache.org "Apache Kafka Index"). Используется
    тестами `ConfluenceJsonDecoder` — они получают raw REST-JSON в
    `RawDocument.handle` и проверяют extraction в metadata + handle-rewind.

    Тело httpx-response материализуется в `BytesIO` ещё внутри generator'а
    transport'а (пока httpx-context открыт) — иначе при выходе из
    generator'а стрим закрывается, и `decoder.convert(...)` падает с
    `httpx.StreamClosed`.
    """
    from io import BytesIO

    from boba.tool.kb.confluence.request_sources.pages import (
        ConfluencePagesRequestSource,
    )

    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_id = test_cfg.confluence_page_ids[0]
    src = ConfluencePagesRequestSource(
        base_url=confluence_cfg.base_url,
        auth=confluence_auth,
        page_ids=[page_id],
        body_format=confluence_cfg.body_format,
    )
    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == 1

    materialized: RawDocument | None = None
    for rd in confluence_transport.stream(pipeline_ctx, requests):
        body = rd.handle.read()
        materialized = replace(rd, handle=BytesIO(body))
    assert materialized is not None
    return materialized


@pytest.fixture
def confluence_decoded_doc(
    confluence_cfg: ConfluenceConnectionConfig,
    confluence_raw_doc,
):
    """Реальный `RawDocument` после `ConfluenceJsonDecoder` (HTML + metadata).

    Используется тестами `ConfluenceReader` — они получают HTML-handle
    с реальной cwiki-страницы и проверяют heading-split / metadata-carry.
    """
    from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder

    decoder = ConfluenceJsonDecoder(body_format=confluence_cfg.body_format)
    return decoder.convert(confluence_raw_doc)


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
