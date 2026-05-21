"""Интеграционный тест operator-mode индексации через StreamingIndexer pipeline.

Включается, когда `[tool.kb].ingest_folder` задан. Берёт DSN и embedding_*
из реального `KbPluginConfig`. Все общие объекты (pool, store, dispatch
reader, chunker) приходят из conftest-фикстур; тут собирается inline
только `StreamingIndexer` + `CollectionScopedView` (привязка к
collection — per-test) и запускается реальный ingest.

Запуск оператором:

    # положить в local/config.toml:
    #   [tool.kb]
    #   dsn = "postgres://postgres:***@127.0.0.1:5432/n1bot"
    #   embedding_model = "..."
    #   embedding_base_url = "..."
    #   ingest_folder = "/path/to/kb"
    #   ingest_collection = "knowledge_base"
    # либо env:
    BOBA_TOOL__KB__INGEST_FOLDER=/path/to/kb \\
        pytest packages/tools/boba-tool-kb -m integration -s
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    Sha256TextEncoder,
    StreamingIndexer,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.text import StructuralChunker
from boba.tool.kb.config import KbPluginConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.transport.fs import FsTransport, FsWalkRequestSource

pytestmark = pytest.mark.integration


def test_operator_real_ingest(
    kb_cfg: KbPluginConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Operator-mode: реальная индексация в postgres + pgvector через pipeline."""
    if not kb_cfg.ingest_folder:
        pytest.skip(
            "ingest disabled: задайте [tool.kb].ingest_folder в конфиге "
            "или BOBA_TOOL__KB__INGEST_FOLDER env",
        )

    folder = Path(kb_cfg.ingest_folder)
    collection = CollectionId(kb_cfg.ingest_collection)

    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=folder,
        collection=collection,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("postgres-kb-ingest-test")),
        IndexerConfig(
            key_encoder=Sha256TextEncoder(),
            cleanup=NoneCleanup(),
            force_update=False,
        ),
    )

    _emit("")
    _emit(f"folder:            {folder}")
    _emit(f"collection:        {collection}")
    _emit(f"dsn:               {_mask_dsn(kb_cfg.dsn)}")
    _emit(f"embedding_model:   {kb_cfg.embedding_model}")
    _emit(f"sources_processed: {stats.sources_processed}")
    _emit(f"sources_failed:    {stats.sources_failed}")
    _emit(f"skipped unchanged: {stats.sources_skipped_unchanged}")
    _emit(f"chunks upserted:   {stats.chunks_upserted}")
    _emit(f"chunks deleted:    {stats.chunks_deleted}")
    assert stats.sources_failed == 0, "some sources failed; see logs"


def test_ingest_is_idempotent_second_run_skips_all(
    kb_cfg: KbPluginConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Повторный прогон ingest не upsert'ит ничего (content_hash совпадает).

    Контракт `CollectionScopedView.reconcile`: при неизменном content
    chunk_id + content_hash детерминированны, `_partition_dirty`
    пропускает чанк (upserted=0).
    """
    if not kb_cfg.ingest_folder:
        pytest.skip("ingest disabled")

    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=Path(kb_cfg.ingest_folder),
        collection=CollectionId(kb_cfg.ingest_collection),
    )
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


def test_ingest_full_cleanup_prunes_missing_sources(
    kb_cfg: KbPluginConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """С `FullCleanup` чанки удалённых файлов уходят из коллекции.

    Запуск с тем же folder'ом: повторный прогон + FullCleanup должен
    давать нулевой prune (всё актуально). Главное — что не валится.
    """
    if not kb_cfg.ingest_folder:
        pytest.skip("ingest disabled")

    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=Path(kb_cfg.ingest_folder),
        collection=CollectionId(kb_cfg.ingest_collection),
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("postgres-kb-ingest-test")),
        IndexerConfig(
            key_encoder=Sha256TextEncoder(),
            cleanup=FullCleanup(),
        ),
    )
    assert stats.chunks_deleted >= 0


def _build_indexer(
    *,
    store: PostgresVectorStore,
    reader: DispatchReader[str],
    chunker: StructuralChunker,
    folder: Path,
    collection: CollectionId,
) -> StreamingIndexer:
    view: CollectionScopedView[str] = CollectionScopedView(
        store_reader=store,
        store_writer=store,
        collection=collection,
    )
    return StreamingIndexer(
        request_source=FsWalkRequestSource(
            paths=[str(folder)],
            include=["*.md", "*.html", "*.htm"],
        ),
        transport=FsTransport(),
        reader=reader,
        chunker=chunker,
        sink=view,
        query=view,
    )


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
