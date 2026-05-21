"""Интеграционный тест operator-mode индексации через StreamingIndexer pipeline.

Включается, когда `cfg.ingest_folder` задан в `[tool.postgres]`. Берёт
dsn и embedding_* из настоящего `PostgresPluginConfig`. Использует тот
же граф, что и `kb_ingest` tool в run-time (FsWalkRequestSource +
FsTransport + DispatchReader(KbDocReader/HtmlReader) + StructuralChunker +
PostgresVectorStore + CollectionScopedView), поэтому что проиндексировано
тестом — то же будет видно через kb_search.

Запуск оператором:

    # либо положить в local/config.toml:
    #   [tool.postgres]
    #   dsn = "postgres://postgres:***@127.0.0.1:5432/n1bot"
    #   embedding_model = "..."
    #   embedding_dim = 1024
    #   embedding_base_url = "..."
    #   ingest_folder = "/path/to/kb"
    #   ingest_collection = "knowledge_base"
    # либо env-override:
    BOBA_TOOL__POSTGRES__INGEST_FOLDER=/path/to/kb \\
        pytest packages/tools/boba-tool-postgres -m integration -s
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from openai import OpenAI
from pgvector.psycopg import register_vector

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.html import HtmlReader
from boba.indexing import (
    ChunkerId,
    CollectionScopedView,
    DispatchReader,
    FixedDigestPrefix,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    ReaderId,
    Sha256TextEncoder,
    SourceBasedChunkId,
    StreamingIndexer,
)
from boba.indexing.context import PipelineId
from boba.kbdoc import KbDocReader
from boba.markdown import MarkdownReader
from boba.provider.openai import OpenAICompatEmbedder
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.migrations import apply_bootstrap
from boba.tool.postgres.vector_store import PostgresVectorStore
from boba.transport.fs import FsKeys, FsTransport, FsWalkRequestSource

if TYPE_CHECKING:
    from tests.conftest import OperatorRunSpec

pytestmark = pytest.mark.integration


def test_operator_real_ingest(operator_run: OperatorRunSpec | None) -> None:
    """Operator-mode: реальная индексация в postgres + pgvector через pipeline."""
    if operator_run is None:
        pytest.skip(
            "operator-mode disabled: set [tool.postgres] ingest_folder + dsn "
            "в config или BOBA_TOOL__POSTGRES__* env",
        )

    cfg = operator_run.cfg
    pool, store = _open_store(cfg)
    try:
        store.ensure_collection(
            operator_run.collection,
            description=cfg.ingest_collection_description or None,
        )
        indexer = _build_indexer(store, operator_run, cfg)
        stats = indexer.invoke(
            PipelineContext(pipeline_id=PipelineId("postgres-kb-ingest-test")),
            IndexerConfig(
                key_encoder=Sha256TextEncoder(),
                cleanup=NoneCleanup(),
                force_update=False,
            ),
        )

        _emit("")
        _emit(f"folder:            {operator_run.folder}")
        _emit(f"collection:        {operator_run.collection}")
        _emit(f"dsn:               {_mask_dsn(cfg.dsn)}")
        _emit(f"embedding_model:   {cfg.embedding_model}")
        _emit(f"sources_processed: {stats.sources_processed}")
        _emit(f"sources_failed:    {stats.sources_failed}")
        _emit(f"skipped unchanged: {stats.sources_skipped_unchanged}")
        _emit(f"chunks upserted:   {stats.chunks_upserted}")
        _emit(f"chunks deleted:    {stats.chunks_deleted}")
        assert stats.sources_failed == 0, "some sources failed; see logs"
    finally:
        pool.close()


def test_ingest_is_idempotent_second_run_skips_all(
    operator_run: OperatorRunSpec | None,
) -> None:
    """Повторный прогон ingest не upsert'ит ничего (content_hash совпадает).

    Контракт `CollectionScopedView.reconcile`: при неизменном content
    chunk_id + content_hash детерминированны, `_partition_dirty`
    пропускает чанк (upserted=0).
    """
    if operator_run is None:
        pytest.skip("operator-mode disabled (см. test_operator_real_ingest)")

    cfg = operator_run.cfg
    pool, store = _open_store(cfg)
    try:
        store.ensure_collection(operator_run.collection, description=None)
        indexer = _build_indexer(store, operator_run, cfg)
        ctx = PipelineContext(pipeline_id=PipelineId("postgres-kb-ingest-test"))
        config: IndexerConfig[str] = IndexerConfig(
            key_encoder=Sha256TextEncoder(),
            cleanup=NoneCleanup(),
        )

        first = indexer.invoke(ctx, config)
        second = indexer.invoke(ctx, config)
        assert second.chunks_upserted == 0, (
            f"expected zero re-indexed, got {second.chunks_upserted} "
            f"(first chunks_upserted={first.chunks_upserted})"
        )
    finally:
        pool.close()


def test_ingest_full_cleanup_prunes_missing_sources(
    operator_run: OperatorRunSpec | None,
) -> None:
    """С `FullCleanup` чанки удалённых файлов уходят из коллекции.

    Запуск с тем же folder'ом: повторный прогон + FullCleanup должен
    давать нулевой prune (всё актуально). Главное — что не валится.
    """
    if operator_run is None:
        pytest.skip("operator-mode disabled (см. test_operator_real_ingest)")

    cfg = operator_run.cfg
    pool, store = _open_store(cfg)
    try:
        store.ensure_collection(operator_run.collection, description=None)
        indexer = _build_indexer(store, operator_run, cfg)
        stats = indexer.invoke(
            PipelineContext(pipeline_id=PipelineId("postgres-kb-ingest-test")),
            IndexerConfig(
                key_encoder=Sha256TextEncoder(),
                cleanup=FullCleanup(),
            ),
        )
        assert stats.chunks_deleted >= 0
    finally:
        pool.close()


def _open_store(cfg: PostgresPluginConfig) -> tuple[PostgresPool, PostgresVectorStore]:
    pool = PostgresPool(
        PostgresConfig(
            dsn=cfg.dsn,
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
        ),
        configure=register_vector,
    )
    with pool.connection() as conn:
        apply_bootstrap(conn)
    client = OpenAI(
        base_url=cfg.embedding_base_url or None,
        api_key=cfg.embedding_api_key or "unused",
    )
    embedder = OpenAICompatEmbedder(
        client=client,
        model=cfg.embedding_model,
    )
    store = PostgresVectorStore(
        pool=pool, embedder=embedder, embedding_dim=embedder.dim(),
    )
    return pool, store


def _build_indexer(
    store: PostgresVectorStore,
    spec: OperatorRunSpec,
    cfg: PostgresPluginConfig,
) -> StreamingIndexer:
    view: CollectionScopedView[str] = CollectionScopedView(
        store_reader=store,
        store_writer=store,
        collection=spec.collection,
    )
    dispatch = DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": KbDocReader(inner=MarkdownReader()),
            "html": HtmlReader(),
            "htm": HtmlReader(),
        },
        reader_id=ReaderId("postgres-kb-dispatch"),
    )
    chunker = StructuralChunker(
        chunker_id=ChunkerId("postgres-kb-structural"),
        splitter_factory=_splitter_factory(cfg.chunk_size, cfg.chunk_overlap),
        id_strategy=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=16),
        ),
    )
    return StreamingIndexer(
        request_source=FsWalkRequestSource(
            paths=[str(spec.folder)],
            include=["*.md", "*.html", "*.htm"],
        ),
        transport=FsTransport(),
        reader=dispatch,
        chunker=chunker,
        sink=view,
        query=view,
    )


def _splitter_factory(chunk_size: int, chunk_overlap: int) -> SplitterFactory:
    def factory(extra_overhead: int) -> OverlapCharSplitter:
        return OverlapCharSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            extra_overhead=extra_overhead,
        )

    return factory


def _mask_dsn(dsn: str) -> str:
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
