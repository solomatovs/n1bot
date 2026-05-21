"""Tool `files_ingest`: индексация pre-настроенной оператором папки в KB.

LLM-facing wrapper над `StreamingIndexer`. Оператор закрепляет folder
(`[tool.kb].files_folder`, default `./local/docs`) и collection
(`[tool.kb].collection`, default `knowledge_base`) — LLM не выбирает,
во что и откуда индексировать, только опционально включает
`prune_missing` для cleanup'а удалённых файлов.

Поддерживаемые форматы (диспатч по `FsKeys.SUFFIX` через `DispatchReader`):
- `.md`         → `KbDocReader(inner=MarkdownReader)` (header + body)
- `.html/.htm`  → `HtmlReader` (heading-aware)

Pipeline собирается inline здесь, потому что зависит от per-call
параметров (cleanup-стратегия). Backend-deps (`PostgresVectorStore`,
`Embedder`, `DispatchReader`, `StructuralChunker`) живут как APP-scope
синглтоны в [providers.py](providers.py).
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
from boba.tool.kb.config import KbConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.fs import FsRequest, FsTransport, FsWalkRequestSource

__all__ = ["files_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.files_ingest")
_INCLUDE_PATTERNS: tuple[str, ...] = ("*.md", "*.html", "*.htm")


@tool
def files_ingest(
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    dispatch_reader: Annotated[DispatchReader[str], FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    cfg: Annotated[KbConfig, FromConfig()],
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
    """Индексирует pre-настроенную оператором папку (`[tool.kb].files_folder`)
    в pre-настроенную коллекцию (`[tool.kb].collection`).

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed}.
    """
    folder = Path(cfg.files_folder)
    if not folder.exists():
        msg = f"folder_not_found: {folder}"
        raise RuntimeError(msg)
    if not folder.is_dir():
        msg = f"folder_not_a_directory: {folder}"
        raise RuntimeError(msg)

    collection = CollectionId(cfg.collection)
    store.ensure_collection(
        collection,
        description=cfg.collection_description or None,
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
