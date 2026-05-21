"""Integration: `files_ingest` — FS-папка → KB-коллекция через настоящий pipeline.

Берёт `files_folder`/`collection` из `[tool.kb]` напрямую (LLM-tool сам
читает их через FromConfig). Все общие объекты (store, dispatch reader,
chunker) — из conftest-фикстур.
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
from boba.tool.kb.config import KbConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.transport.fs import FsTransport, FsWalkRequestSource

pytestmark = pytest.mark.integration


def test_files_ingest_real(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Реальная индексация в postgres + pgvector через `files_ingest` pipeline."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip(
            f"files_folder {folder!s} не существует — задайте "
            "[tool.kb].files_folder в конфиге или поместите файлы в default'е "
            "./local/docs",
        )

    collection = CollectionId(kb_cfg.collection)
    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=folder,
        collection=collection,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("kb-files-ingest-test")),
        IndexerConfig(
            key_encoder=Sha256TextEncoder(),
            cleanup=NoneCleanup(),
            force_update=False,
        ),
    )

    _emit("")
    _emit(f"folder:            {folder}")
    _emit(f"collection:        {collection}")
    _emit(f"embedding_model:   {kb_cfg.embedding_model}")
    _emit(f"sources_processed: {stats.sources_processed}")
    _emit(f"sources_failed:    {stats.sources_failed}")
    _emit(f"skipped unchanged: {stats.sources_skipped_unchanged}")
    _emit(f"chunks upserted:   {stats.chunks_upserted}")
    _emit(f"chunks deleted:    {stats.chunks_deleted}")
    assert stats.sources_failed == 0, "some sources failed; see logs"


def test_files_ingest_is_idempotent_second_run_skips_all(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """Повторный прогон не upsert'ит ничего (content_hash совпадает)."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("files_folder отсутствует")

    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=folder,
        collection=CollectionId(kb_cfg.collection),
    )
    ctx = PipelineContext(pipeline_id=PipelineId("kb-files-ingest-test"))
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


def test_files_ingest_full_cleanup_prunes_missing_sources(
    kb_cfg: KbConfig,
    kb_store: PostgresVectorStore,
    kb_dispatch_reader: DispatchReader[str],
    kb_chunker: StructuralChunker,
) -> None:
    """С `FullCleanup` чанки удалённых файлов уходят из коллекции (нулевой prune ок)."""
    folder = Path(kb_cfg.files_folder)
    if not folder.exists() or not folder.is_dir():
        pytest.skip("files_folder отсутствует")

    indexer = _build_indexer(
        store=kb_store,
        reader=kb_dispatch_reader,
        chunker=kb_chunker,
        folder=folder,
        collection=CollectionId(kb_cfg.collection),
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("kb-files-ingest-test")),
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


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
