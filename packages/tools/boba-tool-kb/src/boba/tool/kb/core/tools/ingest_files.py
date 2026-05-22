"""Tool `files_ingest`: индексация настроенной оператором папки в KB.

Поддерживаемые форматы:
- `.md`         → `KbDocReader` (header + body как одна Section)
- `.html/.htm`  → `HtmlReader` (heading-aware)
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    StreamingIndexer,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.text import StructuralChunker
from boba.tool.kb.core.files_ingest_config import IngestFilesConfig
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.fs import FsRequest, FsTransport, FsWalkRequestSource

__all__ = ["ingest_files"]


@tool
def ingest_files(  # noqa: PLR0913
    chunk_store: Annotated[PostgresChunkStore, FromDI(Scope.APP)],
    collections_store: Annotated[PostgresCollectionsStore, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    dispatch_reader: Annotated[DispatchReader[str], FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    cfg: Annotated[IngestFilesConfig, FromConfig()],
) -> dict[str, Any]:
    """
    Индексирует папку [kb.ingest_files].folder

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed}.
    """
    folder = Path(cfg.folder)
    if not folder.exists():
        msg = f"folder_not_found: {folder}"
        raise RuntimeError(msg)
    if not folder.is_dir():
        msg = f"folder_not_a_directory: {folder}"
        raise RuntimeError(msg)

    collection = CollectionId(cfg.collection)
    collections_store.ensure_collection(collection, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection,
    )
    indexer: StreamingIndexer[FsRequest, str] = StreamingIndexer(
        request_source=FsWalkRequestSource(
            paths=[str(folder)],
            include=["*.md", "*.html", "*.htm"],
        ),
        transport=FsTransport(),
        reader=dispatch_reader,
        chunker=chunker,
        sink=view,
        query=view,
    )
    config: IndexerConfig[str] = IndexerConfig(
        cleanup=FullCleanup() if cfg.prune else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("kb.ingest_files")),
        config,
    )

    return {
        "folder": str(folder),
        "collection": str(collection),
        "indexed": stats.chunks_upserted,
        "skipped_unchanged": stats.sources_skipped_unchanged,
        "pruned": stats.chunks_deleted,
        "failed": stats.sources_failed,
    }
