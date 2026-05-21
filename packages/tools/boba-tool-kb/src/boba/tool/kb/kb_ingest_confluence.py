"""Tool: индексация Confluence-источника в pgvector-коллекцию.

Симметрично `kb_ingest` (FS-вариант): operator фиксирует source +
collection в `[tool.kb.confluence_ingest]`, LLM управляет только
`prune_missing`.

Pipeline:

    {Pages|Cql|Space}RequestSource
        → HttpTransport(timeout, ssl_verify)
        → ConfluenceJsonDecoder(body_format)     ← decoders=[…]
        → ConfluenceReader (heading-aware HTML → Section[str])
        → StructuralChunker
        → CollectionScopedView (sink/query)
        → PostgresVectorStore

`StructuralChunker`, `PostgresVectorStore` и chunk-id стратегия —
APP-scope синглтоны из `providers.py` (тот же граф, что и FS-ingest);
HTTP-часть (transport + auth + RequestSource) собирается inline здесь,
потому что зависит от per-call ingest-параметров (source_type, space_key,
cql, page_ids, body_format).

Используется новый `decoders: Sequence[Decoder]`-параметр у
`StreamingIndexer`: REST-JSON → HTML-handle декодируется до того, как
ConfluenceReader увидит RawDocument.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing import (
    CollectionScopedView,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    Sha256TextEncoder,
    StreamingIndexer,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.text import StructuralChunker
from boba.tool.kb.confluence.config import ConfluencePluginConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.reader import ConfluenceReader
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceSpaceRequestSource,
)
from boba.tool.kb.confluence_ingest_config import ConfluenceIngestConfig
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.http import HttpRequest

__all__ = ["kb_ingest_confluence"]

_PIPELINE_ID: PipelineId = PipelineId("postgres-kb-ingest-confluence")


@tool
def kb_ingest_confluence(
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    conn_cfg: Annotated[ConfluencePluginConfig, FromConfig()],
    ingest_cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди скачанных Confluence-страниц "
                "(cleanup удалённых документов)."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует pre-настроенный Confluence-источник в pre-настроенную коллекцию.

    Возвращает JSON: {source_type, collection, indexed, skipped_unchanged,
    pruned, failed}.
    """
    request_source = _make_request_source(conn_cfg, ingest_cfg)
    transport = ConfluenceConnection.make_transport(conn_cfg)
    decoder = ConfluenceJsonDecoder(body_format=conn_cfg.body_format)
    reader = ConfluenceReader()

    collection = CollectionId(ingest_cfg.collection)
    store.ensure_collection(
        collection,
        description=ingest_cfg.collection_description or None,
    )

    view: CollectionScopedView[str] = CollectionScopedView(
        store_reader=store,
        store_writer=store,
        collection=collection,
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
        key_encoder=Sha256TextEncoder(),
        cleanup=FullCleanup() if prune_missing else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=_PIPELINE_ID),
        config,
    )

    return {
        "source_type": ingest_cfg.source_type,
        "collection": str(collection),
        "indexed": stats.chunks_upserted,
        "skipped_unchanged": stats.sources_skipped_unchanged,
        "pruned": stats.chunks_deleted,
        "failed": stats.sources_failed,
    }


def _make_request_source(
    conn_cfg: ConfluencePluginConfig,
    ingest_cfg: ConfluenceIngestConfig,
):
    """RequestSource по `source_type`. Cross-field валидация полей —
    в `ConfluenceIngestConfig._validate` (fail-fast при загрузке)."""
    auth = ConfluenceConnection.make_auth(conn_cfg)
    match ingest_cfg.source_type:
        case "space":
            return ConfluenceSpaceRequestSource(
                base_url=conn_cfg.base_url,
                auth=auth,
                space_key=ingest_cfg.space_key,
                body_format=conn_cfg.body_format,
                timeout_sec=conn_cfg.timeout_sec,
            )
        case "cql":
            return ConfluenceCqlRequestSource(
                base_url=conn_cfg.base_url,
                auth=auth,
                cql=ingest_cfg.cql,
                body_format=conn_cfg.body_format,
                timeout_sec=conn_cfg.timeout_sec,
            )
        case "pages":
            return ConfluencePagesRequestSource(
                base_url=conn_cfg.base_url,
                auth=auth,
                page_ids=ingest_cfg.page_ids,
                body_format=conn_cfg.body_format,
            )
