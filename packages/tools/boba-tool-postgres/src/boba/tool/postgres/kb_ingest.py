"""Tool: индексация заранее настроенной оператором папки в коллекцию.

LLM-facing wrapper над `StreamingIndexer` из `boba.indexing`. Оператор
закрепляет folder и collection за собой через `[tool.postgres]` (поля
`ingest_folder` / `ingest_collection` / `ingest_collection_description`) —
LLM не выбирает, во что и откуда индексировать, только опционально
включает `prune_missing`.

Поддерживаемые форматы (диспатч по `FsKeys.SUFFIX` через `DispatchReader`):
- `.md`         → `KbDocReader(inner=MarkdownReader)` (header + body)
- `.html/.htm`  → `HtmlReader` (heading-aware)

Pipeline собирается inline здесь, потому что зависит от per-call
параметров (folder, collection, cleanup-стратегия). Backend-deps
(`PostgresVectorStore`, `Embedder`, `DispatchReader`, `StructuralChunker`)
живут как APP-scope синглтоны в [providers.py](providers.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

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
from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.fs import FsRequest, FsTransport, FsWalkRequestSource

__all__ = ["kb_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("postgres-kb-ingest")
_INCLUDE_PATTERNS: tuple[str, ...] = ("*.md", "*.html", "*.htm")


@tool
def kb_ingest(
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    dispatch_reader: Annotated[DispatchReader[str], FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди индексируемых файлов (cleanup удалённых документов)."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует pre-настроенную оператором папку в pre-настроенную коллекцию.

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed}.
    """
    folder = Path(cfg.ingest_folder)
    if not cfg.ingest_folder:
        msg = "postgres.ingest_folder is empty; ingest disabled"
        raise RuntimeError(msg)
    if not folder.exists():
        msg = f"folder_not_found: {folder}"
        raise RuntimeError(msg)
    if not folder.is_dir():
        msg = f"folder_not_a_directory: {folder}"
        raise RuntimeError(msg)

    collection = CollectionId(cfg.ingest_collection)
    store.ensure_collection(
        collection,
        description=cfg.ingest_collection_description or None,
    )

    view: CollectionScopedView[str] = CollectionScopedView(
        store_reader=store,
        store_writer=store,
        collection=collection,
    )
    indexer: StreamingIndexer[FsRequest, str] = StreamingIndexer(
        request_source=FsWalkRequestSource(
            paths=[str(folder)],
            include=list(_INCLUDE_PATTERNS),
        ),
        transport=FsTransport(),
        reader=dispatch_reader,
        chunker=chunker,
        sink=view,
        query=view,
    )
    config: IndexerConfig[str] = IndexerConfig(
        key_encoder=Sha256TextEncoder(),
        cleanup=FullCleanup() if prune_missing else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=_PIPELINE_ID),
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
