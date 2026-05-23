"""Общая сборка Confluence-ingest pipeline для unified-tool `confluence_ingest`.

Три режима tool'а (`space_keys` / `page_ids` / `cql`) делают одно и то же:
берут `RequestSource` (по выбранному режиму), гонят через
`ConfluenceContentTransport` (HTTP + JSON-decode + attachment fan-out) →
`DispatchReader` по `CONTENT_TYPE` → `StructuralChunker` →
`CollectionScopedView` → `PostgresChunkStore`.

`DispatchReader.on_unknown="skip"` — поток смешанный (HTML-страницы +
произвольные attachment'ы); индексируем только то, для чего знаем Reader.
PDF/картинки/прочие бинари молча пропускаются — это not-an-error.

Эта функция инкапсулирует общий хвост — `confluence_ingest` сам выбирает
конкретный `RequestSource` по дискриминатору и зовёт `run_confluence_ingest`.
"""

from __future__ import annotations

from typing import Any

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    RequestSource,
    StreamingIndexer,
    TransportKeys,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.indexing.reader import ReaderId
from boba.text import StructuralChunker
from boba.tool.kb.confluence._pipeline_common import make_confluence_transport
from boba.tool.kb.confluence.attachments import AttachmentFilter
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.reader import ConfluenceReader
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.transport.http import HttpRequest

__all__ = ["run_confluence_ingest"]


_CONFLUENCE_HTML_CONTENT_TYPES = ("text/html",)
"""CONTENT_TYPE-значения, которые `ConfluenceJsonDecoder` ставит после
распаковки JSON→HTML; всё, что попадает под эти ключи, идёт в HTML-Reader."""


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
    attachment_filter: AttachmentFilter | None = None,
) -> dict[str, Any]:
    """Полный Confluence → kb_chunks pipeline для уже-собранного `RequestSource`.

    Возвращает JSON-stats с полями collection/indexed/skipped_unchanged/
    pruned/failed. Caller добавляет свои поля (space_key/page_ids/...).

    Reader — `DispatchReader` по `TransportKeys.CONTENT_TYPE`: HTML
    обрабатывается `ConfluenceReader`'ом, всё остальное (attachment'ы PDF/
    image/etc.) молча пропускается. Когда появятся Reader'ы для PDF или
    картинок, их можно подключить добавив entry в `routes`-mapping.
    """
    confluence_reader = ConfluenceReader()
    reader: DispatchReader[str] = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes={ct: confluence_reader for ct in _CONFLUENCE_HTML_CONTENT_TYPES},
        reader_id=ReaderId("ext.confluence_dispatch"),
        on_unknown="skip",
    )

    collection_id = CollectionId(collection)
    collections_store.ensure_collection(collection_id, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection_id,
    )
    indexer: StreamingIndexer[HttpRequest, str] = StreamingIndexer(
        request_source=request_source,
        transport=make_confluence_transport(
            conn, attachment_filter=attachment_filter,
        ),
        decoders=(),
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
