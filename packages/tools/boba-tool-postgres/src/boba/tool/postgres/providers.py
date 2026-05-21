"""DI-провайдеры postgres KB-плагина.

Граф зависимостей:

    PostgresPluginConfig (FromConfig)
        │
        ├──> ConnectionPool (psycopg_pool, generator с close + bootstrap)
        │       │
        │       └──> apply_bootstrap(conn) на первом подключении
        │            (создаёт kb_chunks/kb_collections если их нет)
        │
        ├──> EmbedderFactory (stateless)
        ├──> Embedder[str]                    # OpenAIEmbedder
        ├──> PostgresVectorStore (pool + Embedder + dim)
        ├──> PostgresKnowledgeBase (pool + Embedder + read-side params)
        ├──> MdChunkParser
        ├──> HtmlChunkParser
        └──> FolderIndexer (store + [Md, Html] парсеры)

Все провайдеры `Scope.APP` — синглтоны на lifetime агента. `ConnectionPool`
— generator-provider с teardown: `pool.close()` гарантированно зовётся
на `Agent.close()`.

pgvector-типы регистрируются на каждом connection через `configure`-hook
ConnectionPool'а — без этого psycopg вернёт `embedding` как plain
строку, а INSERT vector упадёт.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from boba.indexing.embedder import Embedder
from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.embedder_factory import EmbedderFactory
from boba.tool.postgres.folder_indexer import FolderIndexer
from boba.tool.postgres.html_chunk import HtmlChunkParser
from boba.tool.postgres.kb import PostgresKnowledgeBase
from boba.tool.postgres.md_chunk import MdChunkParser
from boba.tool.postgres.migrations import apply_bootstrap
from boba.tool.postgres.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, provides

__all__ = [
    "provide_embedder",
    "provide_embedder_factory",
    "provide_folder_indexer",
    "provide_html_chunk_parser",
    "provide_knowledge_base",
    "provide_md_chunk_parser",
    "provide_postgres_pool",
    "provide_vector_store",
]


def _configure_connection(conn: Any) -> None:
    """`configure`-hook ConnectionPool'а: регистрирует pgvector types.

    Без этого `embedding`-колонка приходит как `str` (текстовая repr
    vector'а), upsert падает на cast.
    """
    register_vector(conn)


@provides(scope=Scope.APP)
def provide_postgres_pool(
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
) -> Iterator[ConnectionPool[Any]]:
    """psycopg_pool.ConnectionPool с teardown + bootstrap-миграциями.

    На первом полученном соединении выполняется `apply_bootstrap`:
    идемпотентные CREATE TABLE/INDEX/EXTENSION. После этого пул отдан
    в DI как обычная APP-scope зависимость.
    """
    pool: ConnectionPool[Any] = ConnectionPool(
        conninfo=cfg.dsn,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        configure=_configure_connection,
        open=True,
    )
    try:
        with pool.connection() as conn:
            apply_bootstrap(conn)
        yield pool
    finally:
        pool.close()


@provides(scope=Scope.APP)
def provide_embedder_factory() -> EmbedderFactory:
    """Stateless factory; легко переопределить subclass-провайдером."""
    return EmbedderFactory()


@provides(scope=Scope.APP)
def provide_embedder(
    factory: Annotated[EmbedderFactory, FromDI(Scope.APP)],
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
) -> Embedder[str]:
    """`Embedder[str]` для ingest и read-side путей."""
    return factory.create(
        model=cfg.embedding_model,
        dim=cfg.embedding_dim,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )


@provides(scope=Scope.APP)
def provide_vector_store(
    pool: Annotated[ConnectionPool[Any], FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
) -> PostgresVectorStore:
    """Postgres-бэкэнд VectorStore[str] + CollectionsAdmin."""
    return PostgresVectorStore(
        pool=pool,
        embedder=embedder,
        embedding_dim=cfg.embedding_dim,
    )


@provides(scope=Scope.APP)
def provide_knowledge_base(
    pool: Annotated[ConnectionPool[Any], FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
) -> PostgresKnowledgeBase:
    """Read-side KB: list_collections + гибридный search (RRF)."""
    return PostgresKnowledgeBase(
        pool=pool,
        embedder=embedder,
        embedding_dim=cfg.embedding_dim,
        snippet_chars=cfg.snippet_chars,
        fts_language=cfg.fts_language,
        rrf_k=cfg.rrf_k,
        rrf_pool=cfg.rrf_pool,
    )


@provides(scope=Scope.APP)
def provide_md_chunk_parser() -> MdChunkParser:
    """Stateless markdown-парсер (pre-chunked .md = один Chunk[str])."""
    return MdChunkParser()


@provides(scope=Scope.APP)
def provide_html_chunk_parser(
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
) -> HtmlChunkParser:
    """HTML-парсер с auto-split + size-cap + canonical-URL extraction.

    Все production-параметры (`split_levels`, `min/max_chunk_chars`,
    `extract_canonical_url`) тянутся из `[tool.postgres]` — оператор
    тюнит через config.toml или env, без правки кода.
    """
    return HtmlChunkParser(
        split_levels=frozenset(cfg.html_split_levels),
        min_chunk_chars=cfg.html_min_chunk_chars,
        max_chunk_chars=cfg.html_max_chunk_chars,
        extract_canonical_url=cfg.html_extract_canonical_url,
    )


@provides(scope=Scope.APP)
def provide_folder_indexer(
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    md_parser: Annotated[MdChunkParser, FromDI(Scope.APP)],
    html_parser: Annotated[HtmlChunkParser, FromDI(Scope.APP)],
) -> FolderIndexer:
    """Multi-format folder indexer: .md (один-файл-один-чанк) + .html (auto-split)."""
    return FolderIndexer(store=store, parsers=[md_parser, html_parser])
