"""Общая сборка KbDoc-ingest pipeline для CLI и tool'а.

Pipeline: переданный `RequestSource[FsRequest]` → `Transport[FsRequest]` →
`KbDocReader` → operator-configured `chunker` → `CollectionScopedView` →
`PostgresChunkStore`. Источник и транспорт каждый caller собирает сам:

- CLI ([cli.kb.kbdoc.ingest]) — `FsWalkRequestSource` + `FsTransport` по
  абсолютному `folder` из конфига.
- Tool ([tool.kb.kbdoc.ingest]) — `WorkspaceWalkRequestSource` +
  `WorkspaceTransport` поверх `ProjectWorkspaceShell` (workspace-rel пути,
  host-путь наружу не уходит).
"""

from __future__ import annotations

from typing import Any

from boba.indexing import (
    CollectionScopedView,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    RequestSource,
    StreamingIndexer,
    Transport,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.kbdoc import KbDocReader
from boba.text import StructuralChunker
from boba.tool.kb.core.llm_metadata_chunker import LlmMetadataChunker
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.transport.fs import FsRequest

__all__ = ["run_kbdoc_ingest"]


def run_kbdoc_ingest(  # noqa: PLR0913 — keyword-only helper, явный набор deps
    *,
    request_source: RequestSource[FsRequest],
    transport: Transport[FsRequest],
    chunk_store: PostgresChunkStore,
    collections_store: PostgresCollectionsStore,
    embedder: Embedder[str],
    chunker: StructuralChunker,
    collection: str,
    prune_missing: bool,
    pipeline_id: PipelineId,
) -> dict[str, Any]:
    """Полный KbDoc → kb_chunks pipeline для уже-собранных source+transport.

    Возвращает JSON-stats `{collection, indexed, skipped_unchanged, pruned,
    failed}`. Caller добавляет свои поля (folder/paths/...).
    """
    collection_id = CollectionId(collection)
    collections_store.ensure_collection(collection_id, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection_id,
    )
    indexer: StreamingIndexer[FsRequest, str] = StreamingIndexer(
        request_source=request_source,
        transport=transport,
        reader=KbDocReader(),
        chunker=LlmMetadataChunker(chunker),
        sink=view,
        query=view,
    )
    config: IndexerConfig[str] = IndexerConfig(
        cleanup=FullCleanup() if prune_missing else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(PipelineContext(pipeline_id=pipeline_id), config)

    return {
        "collection": str(collection_id),
        "indexed": stats.chunks_upserted,
        "skipped_unchanged": stats.sources_skipped_unchanged,
        "pruned": stats.chunks_deleted,
        "failed": stats.sources_failed,
    }
