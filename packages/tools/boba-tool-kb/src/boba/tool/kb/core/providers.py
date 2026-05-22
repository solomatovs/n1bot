"""DI-провайдеры KB-плагина.

Граф зависимостей:

    PostgresConnectionConfig (FromConfig)
        │
        └──> PostgresPool (boba-db-postgres, configure=register_vector)
             (схема + HNSW-индекс не накатываются здесь — это отдельный
              операторский шаг через CLI `boba.tool.kb.core.cli.bootstrap`)

    EmbeddingConfig (FromConfig)
        └──> Embedder[str]                 # OpenAICompatEmbedder

    ChunkStoreSchemaConfig (FromConfig)   # [tool.kb.chunk_store]
        │                                  # schema + chunks/collections table
        ├──> PostgresChunkStore            # document-уровень (upsert/find/...)
        ├──> PostgresCollectionsStore      # collection-уровень (ensure/delete)
        └──> PostgresKnowledgeBase         # hybrid RRF search

    KbConfig (FromConfig)
        │
        ├──> PostgresChunkStore            # уже подцепляет Embedder из DI
        ├──> PostgresCollectionsStore      # collections-CRUD
        ├──> PostgresKnowledgeBase         # hybrid RRF search
        ├──> KbDocReader                   # header + body как одна Section
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
from boba.provider.openai import OpenAICompatEmbedder
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.embedding_config import EmbeddingConfig
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, FromDI, Scope, provides
from boba.transport.fs import FsKeys

__all__ = [
    "provide_chunk_store",
    "provide_chunker",
    "provide_collections_store",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
]

_DISPATCH_READER_ID: ReaderId = ReaderId("postgres-kb-dispatch")
_CHUNKER_ID: ChunkerId = ChunkerId("postgres-kb-structural")
_CHUNK_ID_PREFIX_LENGTH: int = 16


@provides(scope=Scope.APP)
def provide_postgres_pool(
    pg_cfg: Annotated[PostgresConnectionConfig, FromConfig()],
) -> Iterator[PostgresPool]:
    """PostgresPool с register_vector configure-hook.

    DSN/pool sizes — из `[tool.kb.postgres]` (структурированно).
    pgvector-types регистрируются на каждом свежем connection через
    configure-callback — без этого `embedding`-колонка приходит как
    plain str, upsert vector падает на cast.

    Схема БД (миграции + HNSW-индекс) **не** накатывается здесь — это
    одноразовый операторский шаг: `python -m boba.tool.kb.core.cli.bootstrap`.
    Если схемы нет, ingest/search упадут на SQL-ошибке (table not found) —
    это и есть сигнал оператору запустить bootstrap.
    """
    pool = PostgresPool.get(
        pg_cfg.to_pool_config(),
        configure=register_vector,
    )
    try:
        yield pool
    finally:
        pool.close()


@provides(scope=Scope.APP)
def provide_embedder(
    cfg: Annotated[EmbeddingConfig, FromConfig()],
) -> Embedder[str]:
    """`Embedder[str]` для ingest и read-side путей.

    OpenAI-совместимый endpoint (LiteLLM / OpenAI / vLLM / Ollama)
    через `OpenAICompatEmbedder` — без `dimensions=` параметра, чтобы
    работал с Ollama. Размерность модели определяется lazy probe'ом
    (см. `OpenAICompatEmbedder.dim()`). Валидация `model` — в
    `EmbeddingConfig._validate` (fail-fast на load-time).
    """
    client = OpenAI(
        base_url=cfg.base_url or None,
        api_key=cfg.api_key or "unused",
    )
    return OpenAICompatEmbedder(
        client=client,
        model=cfg.model,
    )


@provides(scope=Scope.APP)
def provide_chunk_store(
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    schema_cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> PostgresChunkStore:
    """Postgres-бэкэнд `ChunkStore[str]` (document-уровень).

    Store сам про Embedder не знает — это чистый layer хранения. Embedder
    тут только чтобы спросить `dim()` для конфигурации vector-колонки;
    вызов embedder'а на write/read делается в pipeline-orchestrator'е
    (`CollectionScopedView` и `PostgresKnowledgeBase`).

    `schema_cfg` ([tool.kb.chunk_store]) — тот же конфиг, что получает
    bootstrap-CLI; гарантирует, что store ходит в те же таблицы, которые
    создал bootstrap.
    """
    return PostgresChunkStore(
        pool=pool,
        embedding_dim=embedder.dim(),
        cfg=schema_cfg,
    )


@provides(scope=Scope.APP)
def provide_collections_store(
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
    schema_cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> PostgresCollectionsStore:
    """Postgres-бэкэнд `CollectionsStore` (collection-уровень CRUD).

    Используется ingest-tool'ами для `ensure_collection(...)` перед
    запуском pipeline. `schema_cfg` тот же, что у `provide_chunk_store`
    и bootstrap-CLI.
    """
    return PostgresCollectionsStore(
        pool=pool,
        cfg=schema_cfg,
    )


@provides(scope=Scope.APP)
def provide_knowledge_base(
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    cfg: Annotated[KbConfig, FromConfig()],
    schema_cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> PostgresKnowledgeBase:
    """Read-side KB: гибридный search (vector + FTS, RRF) для `kb_search`.

    `schema_cfg` — тот же, что в `provide_chunk_store` и bootstrap-CLI;
    hybrid SQL ходит в `chunks_table` и зовёт `schema.immutable_unaccent`.
    """
    return PostgresKnowledgeBase(
        pool=pool,
        embedder=embedder,
        embedding_dim=embedder.dim(),
        snippet_chars=cfg.snippet_chars,
        fts_language=cfg.fts_language,
        rrf_k=cfg.rrf_k,
        rrf_pool=cfg.rrf_pool,
        schema_cfg=schema_cfg,
    )


@provides(scope=Scope.APP)
def provide_kbdoc_reader() -> KbDocReader:
    """KbDoc-формат (`**key:** value` header + body). Один файл = одна Section."""
    return KbDocReader()


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
    - `md`        → KbDocReader (header + body как одна Section)
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
    """Heading-aware Chunker с `OverlapCharSplitter` для size-cap.

    `key_encoder=Sha256TextEncoder()` хэширует `format_content` каждого
    чанка в `content_hash` — это то по чему `IndexSink.reconcile` решает
    skip vs upsert. Отдельный от `id_strategy.encoder` инстанс (тот хэширует
    `source_id` для `chunk_id`), хотя оба используют SHA-256.
    """
    return StructuralChunker(
        chunker_id=_CHUNKER_ID,
        splitter_factory=_make_splitter_factory(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
        ),
        chunk_id_generator=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=_CHUNK_ID_PREFIX_LENGTH),
        ),
        content_hasher=Sha256TextEncoder(),
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
