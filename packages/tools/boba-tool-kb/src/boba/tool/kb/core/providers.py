"""DI-провайдеры KB-плагина.

Граф зависимостей:

    PostgresStoreConfig (FromConfig)    # [tool.kb.postgres_store]
        │                                # connection + schema + tables
        ├──> PostgresPool                # cfg.open_pool() — для fts/sql/...
        ├──> PostgresChunkStore          # сам открывает pool через cfg
        ├──> PostgresCollectionsStore    # сам открывает pool через cfg
        └──> PostgresKnowledgeBase       # сам открывает pool через cfg

    EmbeddingConfig (FromConfig)
        └──> Embedder[str]               # OpenAICompatEmbedder

    KbConfig (FromConfig)                # [tool.kb] — общие search/chunker
        │
        ├──> PostgresKnowledgeBase       # search params (RRF, snippet_chars)
        ├──> KbDocReader / HtmlReader / DispatchReader[str]
        └──> StructuralChunker           # heading-aware + OverlapCharSplitter

Pool-граф: `cfg.open_pool()` использует `PostgresPool.get(...)`-singleton
по DSN, поэтому store / collections-store / KB / fts получают **один и
тот же** pool, пока используют один `PostgresStoreConfig`.

`StreamingIndexer` и `CollectionScopedView` собираются inline в ingest-tool'ах
— они зависят от per-call параметров (source/collection/cleanup), которые
не имеют смысла фиксировать в APP-scope синглтоне.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from openai import OpenAI

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
    cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> Iterator[PostgresPool]:
    """`PostgresPool` под `[tool.kb.postgres_store].connection`.

    Тот же объект, что получают `PostgresChunkStore` / `PostgresCollectionsStore` /
    `PostgresKnowledgeBase` через `cfg.open_pool()` — `PostgresPool.get(...)`
    singleton-кэшируется по DSN. Провайдер нужен сторонним подписчикам
    (например, `fts_search`), которые хотят шарить pool через DI.

    Схема БД (миграции + HNSW-индекс) **не** накатывается здесь — это
    одноразовый операторский шаг: `python -m boba.tool.kb.cli.bootstrap`.
    Если схемы нет, ingest/search упадут на SQL-ошибке (table not found) —
    это и есть сигнал оператору запустить bootstrap.
    """
    pool = PostgresChunkStore.open_pool(cfg)
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
    cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> PostgresChunkStore:
    """Postgres-бэкэнд `ChunkStore[str]` (document-уровень).

    Store сам открывает pool через `cfg.open_pool()` (singleton по DSN).
    Embedder в Store не нужен — это чистый layer хранения; вызов
    embedder'а на write/read делается в pipeline-orchestrator'е
    (`CollectionScopedView` и `PostgresKnowledgeBase`).
    """
    return PostgresChunkStore(cfg=cfg)


@provides(scope=Scope.APP)
def provide_collections_store(
    cfg: Annotated[PostgresStoreConfig, FromConfig()],
) -> PostgresCollectionsStore:
    """Postgres-бэкэнд `CollectionsStore` (collection-уровень CRUD).

    Используется ingest-tool'ами для `ensure_collection(...)` перед
    запуском pipeline. Pool тот же, что у `provide_chunk_store` —
    singleton по DSN.
    """
    return PostgresCollectionsStore(cfg=cfg)


@provides(scope=Scope.APP)
def provide_knowledge_base(
    cfg: Annotated[PostgresStoreConfig, FromConfig()],
    kb_cfg: Annotated[KbConfig, FromConfig()],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
) -> PostgresKnowledgeBase:
    """Read-side KB: гибридный search (vector + FTS, RRF) для `kb_search`.

    `cfg` — тот же `PostgresStoreConfig`, что у `provide_chunk_store` и
    bootstrap-CLI; hybrid SQL ходит в `chunks_table` и зовёт
    `schema.immutable_unaccent`.
    """
    return PostgresKnowledgeBase(
        cfg=cfg,
        embedder=embedder,
        embedding_dim=embedder.dim(),
        snippet_chars=kb_cfg.snippet_chars,
        fts_language=kb_cfg.fts_language,
        rrf_k=kb_cfg.rrf_k,
        rrf_pool=kb_cfg.rrf_pool,
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
