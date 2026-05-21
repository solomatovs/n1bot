"""DI-провайдеры KB-плагина.

Граф зависимостей:

    PostgresConnectionConfig (FromConfig)
        │
        └──> PostgresPool (boba-db-postgres, configure=register_vector)
                │
                └──> apply_bootstrap(conn) на первом подключении
                     (создаёт kb_chunks/kb_collections если их нет)

    KbConfig (FromConfig)
        │
        ├──> Embedder[str]                 # OpenAICompatEmbedder
        ├──> PostgresVectorStore
        ├──> PostgresKnowledgeBase         # hybrid RRF search
        ├──> KbDocReader(inner=MarkdownReader())
        ├──> HtmlReader
        ├──> DispatchReader[str]           # by FsKeys.SUFFIX
        └──> StructuralChunker             # heading-aware + OverlapCharSplitter

`StreamingIndexer` и `CollectionScopedView` собираются inline в ingest-tool'ах
— они зависят от per-call параметров (source/collection/cleanup), которые
не имеют смысла фиксировать в APP-scope синглтоне.

pgvector-типы регистрируются на каждом connection через `configure`-hook
PostgresPool'а — без этого psycopg вернёт `embedding` как plain строку,
а INSERT vector упадёт.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from openai import OpenAI
from pgvector.psycopg import register_vector

from boba.db.postgres import PostgresPool
from boba.html import HtmlReader
from boba.indexing import (
    ChunkerId,
    DispatchReader,
    FixedDigestPrefix,
    ReaderId,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.indexing.embedder import Embedder
from boba.kbdoc import KbDocReader
from boba.markdown import MarkdownReader
from boba.provider.openai import OpenAICompatEmbedder
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.kb.config import KbConfig
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.migrations import apply_bootstrap
from boba.tool.kb.postgres_config import PostgresConnectionConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, provides
from boba.transport.fs import FsKeys

__all__ = [
    "provide_chunker",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
    "provide_vector_store",
]

_DISPATCH_READER_ID: ReaderId = ReaderId("postgres-kb-dispatch")
_CHUNKER_ID: ChunkerId = ChunkerId("postgres-kb-structural")
_CHUNK_ID_PREFIX_LENGTH: int = 16


@provides(scope=Scope.APP)
def provide_postgres_pool(
    pg_cfg: Annotated[PostgresConnectionConfig, FromConfig()],
) -> Iterator[PostgresPool]:
    """PostgresPool с register_vector configure-hook и bootstrap-миграциями.

    DSN/pool sizes — из `[tool.kb.postgres]` (структурированно). На первом
    полученном соединении выполняется `apply_bootstrap`: идемпотентные
    CREATE TABLE/INDEX/EXTENSION. pgvector-types регистрируются на каждом
    свежем connection через configure-callback — без этого `embedding`-
    колонка приходит как plain str, upsert vector падает на cast.
    """
    pool = PostgresPool.get(
        pg_cfg.to_pool_config(),
        configure=register_vector,
    )
    try:
        with pool.connection() as conn:
            apply_bootstrap(conn)
        yield pool
    finally:
        pool.close()


@provides(scope=Scope.APP)
def provide_embedder(
    cfg: Annotated[KbConfig, FromConfig()],
) -> Embedder[str]:
    """`Embedder[str]` для ingest и read-side путей.

    OpenAI-совместимый endpoint (LiteLLM / OpenAI / vLLM / Ollama)
    через `OpenAICompatEmbedder` — без `dimensions=` параметра, чтобы
    работал с Ollama. Размерность модели определяется lazy probe'ом
    (см. `OpenAICompatEmbedder.dim()`).
    """
    if not cfg.embedding_model:
        msg = (
            "kb.embedding_model is empty; задайте "
            "[tool.kb].embedding_model или "
            "BOBA_TOOL__KB__EMBEDDING_MODEL."
        )
        raise ValueError(msg)
    client = OpenAI(
        base_url=cfg.embedding_base_url or None,
        api_key=cfg.embedding_api_key or "unused",
    )
    return OpenAICompatEmbedder(
        client=client,
        model=cfg.embedding_model,
    )


@provides(scope=Scope.APP)
def provide_vector_store(
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
) -> PostgresVectorStore:
    """Postgres-бэкэнд VectorStore[str] + CollectionsAdmin.

    `embedding_dim` берётся у самого `Embedder.dim()` — lazy probe
    при первом вызове, потом кэш.
    """
    return PostgresVectorStore(
        pool=pool,
        embedder=embedder,
        embedding_dim=embedder.dim(),
    )


@provides(scope=Scope.APP)
def provide_knowledge_base(
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    cfg: Annotated[KbConfig, FromConfig()],
) -> PostgresKnowledgeBase:
    """Read-side KB: гибридный search (vector + FTS, RRF) для `kb_search`."""
    return PostgresKnowledgeBase(
        pool=pool,
        embedder=embedder,
        embedding_dim=embedder.dim(),
        snippet_chars=cfg.snippet_chars,
        fts_language=cfg.fts_language,
        rrf_k=cfg.rrf_k,
        rrf_pool=cfg.rrf_pool,
    )


@provides(scope=Scope.APP)
def provide_kbdoc_reader() -> KbDocReader:
    """KbDoc-формат (`**key:** value` header + body) с markdown-body."""
    return KbDocReader(inner=MarkdownReader())


@provides(scope=Scope.APP)
def provide_html_reader() -> HtmlReader:
    """Структурный HTML-Reader (типизированные Section'ы по heading)."""
    return HtmlReader()


@provides(scope=Scope.APP)
def provide_dispatch_reader(
    kbdoc_reader: Annotated[KbDocReader, FromDI(Scope.APP)],
    html_reader: Annotated[HtmlReader, FromDI(Scope.APP)],
) -> DispatchReader[str]:
    """DispatchReader по `FsKeys.SUFFIX` (значения от `FsTransport`).

    Поддерживаемые форматы:
    - `md`        → KbDocReader (header + markdown body)
    - `html/htm`  → HtmlReader (heading-aware)
    """
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": kbdoc_reader,
            "html": html_reader,
            "htm": html_reader,
        },
        reader_id=_DISPATCH_READER_ID,
    )


@provides(scope=Scope.APP)
def provide_chunker(
    cfg: Annotated[KbConfig, FromConfig()],
) -> StructuralChunker:
    """Heading-aware Chunker с `OverlapCharSplitter` для size-cap."""
    return StructuralChunker(
        chunker_id=_CHUNKER_ID,
        splitter_factory=_make_splitter_factory(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
        ),
        id_strategy=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=_CHUNK_ID_PREFIX_LENGTH),
        ),
    )


def _make_splitter_factory(
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> SplitterFactory:
    """Замыкаем chunk_size/chunk_overlap; StructuralChunker дёргает на каждой
    секции с `extra_overhead = len(prefix + repeat_header + repeat_footer)`,
    чтобы итоговый чанк влез в budget."""

    def factory(extra_overhead: int) -> OverlapCharSplitter:
        return OverlapCharSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            extra_overhead=extra_overhead,
        )

    return factory
