"""Общая сборка Confluence-ingest pipeline для `confluence_*_ingest`-тулов.

Различные tool'ы (`confluence_space_ingest`, `confluence_page_ingest`)
делают одно и то же: берут `RequestSource` (по своему типу источника),
гонят через `HttpTransport` → `ConfluenceJsonDecoder` → `ConfluenceReader`
→ `StructuralChunker` → `CollectionScopedView` → `PostgresChunkStore`.

Эта функция инкапсулирует общий хвост — каждый tool сам выбирает
конкретный `RequestSource` и зовёт `run_confluence_ingest`.
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
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.text import StructuralChunker
from boba.tool.kb.confluence._pipeline_common import make_confluence_stages
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.reader import ConfluenceReader
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.transport.http import HttpRequest

__all__ = ["run_confluence_ingest"]


def run_confluence_ingest(  # noqa: PLR0913 — keyword-only helper, явный набор deps
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    chunk_store: PostgresChunkStore,
    collections_store: PostgresCollectionsStore,
    embedder: Embedder[str],
    chunker: StructuralChunker,
    collection: str,
    prune_missing: bool,
    pipeline_id: PipelineId,
) -> dict[str, Any]:
    """Полный Confluence → kb_chunks pipeline для уже-собранного `RequestSource`.

    Возвращает JSON-stats с полями collection/indexed/skipped_unchanged/
    pruned/failed. Caller добавляет свои поля (space_key/page_ids/...).
    """
    transport, decoder = make_confluence_stages(conn)
    reader = ConfluenceReader()

    collection_id = CollectionId(collection)
    collections_store.ensure_collection(collection_id, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection_id,
    )
    indexer: StreamingIndexer[HttpRequest, str] = StreamingIndexer(
        request_source=request_source,
        transport=transport,
        decoders=[decoder],
        reader=reader,
        chunker=chunker,
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
