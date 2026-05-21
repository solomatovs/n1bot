"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

ВСЕ тесты в пакете — integration: ходят в реальный postgres, реальный
embeddings endpoint, реальный Confluence. Параметры конфигурации читаются
через систему конфигурирования (BobaFlatSettings) из:

- `[tool.kb]`                  → KbPluginConfig (dsn, embedder, ingest_*)
- `[tool.kb.confluence]`       → ConfluencePluginConfig (Confluence connection)
- `[tool.kb.confluence_ingest]` → ConfluenceIngestConfig (Confluence ingest source)
- `[tool.kb.external_fts]`     → ExternalFtsConfig (whitelist FTS-индексов)
- `[test.kb]`                  → KbIntegrationTestConfig (тестовые параметры:
                                  page_ids, search-query, fts-index/query, ...)

Каждая конфиг-фикстура skip-ает тест с понятной причиной, если секция
не сконструировалась (validation failed) — это значит оператор не
заполнил соответствующую секцию.

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает — см. root pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from openai import OpenAI
from pgvector.psycopg import register_vector
from pydantic import Field, ValidationError

from boba.db.postgres import PostgresConfig, PostgresPool
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
from boba.tool.kb.config import KbPluginConfig
from boba.tool.kb.confluence.config import ConfluencePluginConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence_ingest_config import ConfluenceIngestConfig
from boba.tool.kb.external_fts.config import ExternalFtsConfig
from boba.tool.kb.external_fts.db import PgFtsKnowledgeBase
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.migrations import apply_bootstrap
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.transport.fs import FsKeys
from boba.transport.http import HttpTransport
from boba.workspace.contract import WorkspaceId

# --------------------------------------------------------------------------- #
# KbIntegrationTestConfig — тестовая секция [test.kb]
# --------------------------------------------------------------------------- #


class KbIntegrationTestConfig(BobaFlatSettings):
    """Параметры integration-тестов KB-плагина (секция `[test.kb]`).

    Отдельный namespace от `[tool.kb.*]`, чтобы тестовые параметры не
    смешивались с продакшен-конфигом плагина. Все поля имеют дефолты или
    отмечены как «нужно для подмножества тестов» — тесты, которым поле
    необходимо, скипаются, если значение пусто.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="test.kb",
    )

    # kb_search
    kb_search_query: str = Field(
        default="",
        description=(
            "Search-запрос для test_kb_search. Пусто = тест skip'нется."
        ),
    )
    kb_search_top_k: int = Field(
        default=5,
        ge=1,
        description="top_k для test_kb_search.",
    )

    # confluence (page_download / page_outline / page_section / requests)
    confluence_page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Реальные page_id для test_confluence_page_download[_markdown]. "
            "Пусто = тесты скипаются."
        ),
    )
    confluence_space_key: str = Field(
        default="",
        description=(
            "Реальный space_key для test_space_source_paginates_via_discovery. "
            "Пусто = тест skip'нется."
        ),
    )
    confluence_cql: str = Field(
        default="",
        description=(
            "Реальный CQL-запрос для test_cql_source_uses_search_endpoint. "
            "Пусто = тест skip'нется."
        ),
    )

    # external_fts
    fts_index_name: str = Field(
        default="",
        description=(
            "Имя индекса из [tool.kb.external_fts].indexes для тестов FTS. "
            "Пусто = FTS-интеграционные тесты скипаются (если такие есть)."
        ),
    )
    fts_query: str = Field(
        default="",
        description="Запрос для тестов FTS. Пусто = FTS-тесты скипаются.",
    )


# --------------------------------------------------------------------------- #
# Config-фикстуры (skip-if-not-configured)
# --------------------------------------------------------------------------- #


@pytest.fixture
def kb_cfg() -> KbPluginConfig:
    """KbPluginConfig из `[tool.kb]`; skip, если секция не сконструировалась."""
    try:
        return KbPluginConfig()
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb] не сконфигурирован (нужны dsn и embedding_model): {e}",
        )


@pytest.fixture
def confluence_cfg() -> ConfluencePluginConfig:
    """ConfluencePluginConfig из `[tool.kb.confluence]`; skip при ошибке."""
    try:
        return ConfluencePluginConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence] не сконфигурирован: {e}")


@pytest.fixture
def confluence_ingest_cfg() -> ConfluenceIngestConfig:
    """ConfluenceIngestConfig из `[tool.kb.confluence_ingest]`; skip при ошибке."""
    try:
        return ConfluenceIngestConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.confluence_ingest] не сконфигурирован: {e}")


@pytest.fixture
def external_fts_cfg() -> ExternalFtsConfig:
    """ExternalFtsConfig из `[tool.kb.external_fts]`; skip при ошибке."""
    try:
        return ExternalFtsConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.kb.external_fts] не сконфигурирован: {e}")


@pytest.fixture
def test_cfg() -> KbIntegrationTestConfig:
    """KbIntegrationTestConfig из `[test.kb]`; skip при ошибке валидации.

    Сам класс почти все поля имеет с дефолтами — load-time validation
    обычно не падает; конкретные тесты сами проверяют непустоту
    нужных им полей и skip'аются точечно.
    """
    try:
        return KbIntegrationTestConfig()
    except ValidationError as e:
        pytest.skip(f"[test.kb] не сконфигурирован: {e}")


# --------------------------------------------------------------------------- #
# Реальные backend-фикстуры (зависят от config-фикстур; те уже скипают тест)
# --------------------------------------------------------------------------- #


@pytest.fixture
def kb_pool(kb_cfg: KbPluginConfig) -> PostgresPool:
    """PostgresPool на DSN из `[tool.kb]` с register_vector + bootstrap-миграциями.

    Singleton-cached `PostgresPool.get(...)` — повторный вызов с тем же
    DSN+pool-sizes возвращает тот же инстанс. Close НЕ зовём здесь
    (cache живёт до process exit; повторное использование между тестами).
    """
    pool = PostgresPool.get(
        PostgresConfig(
            dsn=kb_cfg.dsn,
            min_size=kb_cfg.pool_min_size,
            max_size=kb_cfg.pool_max_size,
        ),
        configure=register_vector,
    )
    with pool.connection() as conn:
        apply_bootstrap(conn)
    return pool


@pytest.fixture
def kb_embedder(kb_cfg: KbPluginConfig) -> OpenAICompatEmbedder:
    """OpenAI-совместимый embedder для kb_search/kb_ingest."""
    client = OpenAI(
        base_url=kb_cfg.embedding_base_url or None,
        api_key=kb_cfg.embedding_api_key or "unused",
    )
    return OpenAICompatEmbedder(client=client, model=kb_cfg.embedding_model)


@pytest.fixture
def kb_store(
    kb_cfg: KbPluginConfig,
    kb_pool: PostgresPool,
    kb_embedder: OpenAICompatEmbedder,
) -> PostgresVectorStore:
    """Write-side store для kb_ingest. `embedding_dim` — lazy probe у embedder'а."""
    del kb_cfg  # явно: store не читает cfg, всё уже в pool/embedder
    return PostgresVectorStore(
        pool=kb_pool,
        embedder=kb_embedder,
        embedding_dim=kb_embedder.dim(),
    )


@pytest.fixture
def kb_knowledge_base(
    kb_cfg: KbPluginConfig,
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
    """DispatchReader для kb_ingest: md → KbDocReader, html/htm → HtmlReader.

    Тот же набор route'ов, что и `provide_dispatch_reader` (см. providers.py)
    — фикстура дублирует, чтобы тест не тянул DI-контейнер.
    """
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": KbDocReader(inner=MarkdownReader()),
            "html": HtmlReader(),
            "htm": HtmlReader(),
        },
        reader_id=ReaderId("postgres-kb-dispatch"),
    )


@pytest.fixture
def kb_chunker(kb_cfg: KbPluginConfig) -> StructuralChunker:
    """StructuralChunker с chunk_size/overlap из `[tool.kb]`."""

    def splitter_factory(extra_overhead: int) -> OverlapCharSplitter:
        return OverlapCharSplitter(
            chunk_size=kb_cfg.chunk_size,
            chunk_overlap=kb_cfg.chunk_overlap,
            extra_overhead=extra_overhead,
        )

    factory: SplitterFactory = splitter_factory
    return StructuralChunker(
        chunker_id=ChunkerId("postgres-kb-structural"),
        splitter_factory=factory,
        id_strategy=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=16),
        ),
    )


@pytest.fixture
def pg_fts_kb(
    external_fts_cfg: ExternalFtsConfig,
    kb_pool: PostgresPool,
) -> PgFtsKnowledgeBase:
    """Read-side FTS KB по whitelist-индексам оператора.

    Pool шарится с `kb_pool` (тот же PostgresPool из singleton-cache —
    `external_fts_cfg.dsn=""` фолбэчится на `[tool.kb].dsn`).
    """
    return PgFtsKnowledgeBase(
        pool=kb_pool,
        indexes=external_fts_cfg.indexes,
        snippet_options=external_fts_cfg.snippet_options,
    )


@pytest.fixture
def confluence_auth(confluence_cfg: ConfluencePluginConfig) -> httpx.Auth | None:
    """Real httpx.Auth (PatAuth | BasicAuth | None) из `ConfluenceConnection.make_auth`.

    `None` при `auth_method=none` — anonymous-доступ к публичному Confluence
    (cwiki.apache.org и т.п.).
    """
    return ConfluenceConnection.make_auth(confluence_cfg)


@pytest.fixture
def confluence_transport(
    confluence_cfg: ConfluencePluginConfig,
) -> HttpTransport:
    """Real HttpTransport с timeout/ssl_verify из конфига Confluence."""
    return ConfluenceConnection.make_transport(confluence_cfg)


@pytest.fixture
def workspace_shell(tmp_path: Path):
    """Real `FsProjectWorkspaceShell` на pytest tmp_path.

    Концретная реализация ProjectWorkspaceShell лежит в `boba.agent.workspace_fs`
    (плагин-инфраструктура). Импорт — внутри фикстуры, чтобы не тащить
    `boba.agent` как обязательную тест-dep, если фикстура не используется.
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
