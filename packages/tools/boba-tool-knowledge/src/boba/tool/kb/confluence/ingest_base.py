"""Общая база Confluence-ingest: конфиг и сборка pipeline'а для confluence_ingest_*."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from boba.db.pgvector import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.indexing import (
    CleanupStrategy,
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    Pipeline,
    Reader,
    RequestSource,
    TransportKeys,
)
from boba.indexing.ports import Embedder, ReaderId
from boba.indexing.values import CollectionId
from boba.text import StructuralChunker
from boba.tool.doc.liteparse import SandboxParserConfig
from boba.tool.kb.chunking import (
    ChunkerParams,
    StructuralChunkerFactory,
)
from boba.tool.kb.confluence.cleanup import ConfluencePageScopeCleanup
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.request_sources import ConfluenceRequest
from boba.tool.kb.embedding import (
    EmbeddingModel,
    LocalFastEmbedEmbedderFactory,
)
from boba.tool.kb.indexing_log import LoggedIndexRun
from boba.toolkit.types import StringList
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceIngest", "ConfluenceIngestConfig"]

logger = logging.getLogger("boba.tool.kb.confluence.ingest")


class ConfluenceIngestConfig(PostgresStoreConfig, ChunkerParams, SandboxParserConfig):
    """Self-contained конфиг семейства tool'ов confluence_ingest_*."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingModel
    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    collection: str = Field(
        default="kb_confluence",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )
    attachment_media_types: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.media_type` "
                "(напр. `application/pdf`, `image/*`). Если пусто И "
                "`attachment_titles` пуст — пропускаются ВСЕ вложения "
                "(старое поведение). Если задано — attachment проходит, "
                "если матчит хоть один паттерн в любом из двух списков (OR)."
            ),
        ),
    ] = []  # noqa: RUF012
    attachment_titles: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.title` "
                "(напр. `*.pdf`, `report-*.docx`). См. `attachment_media_types`."
            ),
        ),
    ] = []  # noqa: RUF012


class ConfluenceIngest:
    """Сборка Confluence-ingest pipeline — общий хвост для confluence_ingest_*."""

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения от ConfluenceJsonDecoder, уходящие в HTML-Reader."""

    @staticmethod
    async def run(  # noqa: PLR0913
        *,
        request_source: RequestSource[ConfluenceRequest],
        conn: ConfluenceConnection,
        chunk_store: PostgresChunkStore,
        collections_store: PostgresCollectionsStore,
        embedder: Embedder[str],
        chunker: StructuralChunker,
        collection: str,
        prune_missing: bool,
        force_update: bool = False,
        attachment_filter: AttachmentFilter | None = None,
        routes: Mapping[str, Reader[str]],
    ) -> dict[str, Any]:
        """Полный Confluence -> kb_chunks pipeline для уже собранного RequestSource."""
        reader: DispatchReader[str] = DispatchReader(
            by=TransportKeys.CONTENT_TYPE,
            routes=dict(routes),
            reader_id=ReaderId("ext.confluence_dispatch"),
            on_unknown="skip",
        )

        collection_id = CollectionId(collection)
        await collections_store.ensure_collection(collection_id, description=None)

        view: CollectionScopedView[str] = CollectionScopedView(
            store=chunk_store,
            embedder=embedder,
            collection=collection_id,
        )
        transport = ConfluenceContentTransport.from_connection(
            conn,
            attachment_filter=attachment_filter,
        )

        # prune_missing сносит весь стейл коллекции; force_update без prune — только страниц
        if prune_missing:
            cleanup: CleanupStrategy = FullCleanup()
        elif force_update:
            cleanup = ConfluencePageScopeCleanup()
        else:
            cleanup = NoneCleanup()

        config: IndexerConfig[str] = IndexerConfig(
            cleanup=cleanup,
            force_update=force_update,
        )
        try:
            pipeline: Pipeline[ConfluenceRequest, str] = Pipeline(
                source=request_source,
                transport=transport,
                reader=reader,
            )
            stats = await LoggedIndexRun.drain(
                pipeline.index(
                    chunker=chunker,
                    sink=view,
                    query=view,
                    config=config,
                ),
                logger,
            )
        finally:
            transport.close()

        return {
            "collection": str(collection_id),
            "indexed": stats.chunks_upserted,
            "skipped_unchanged": stats.sources_skipped_unchanged,
            "pruned": stats.chunks_deleted,
            "failed": stats.sources_failed,
        }

    @staticmethod
    async def ingest(
        cfg: ConfluenceIngestConfig,
        request_source: RequestSource[ConfluenceRequest],
        prune_missing: bool,
        force_update: bool = False,
        *,
        routes: Mapping[str, Reader[str]],
    ) -> dict[str, Any]:
        """Собрать stores/embedder/chunker/filter из cfg и вызвать run."""
        chunk_store = PostgresChunkStore(cfg=cfg)
        collections_store = PostgresCollectionsStore(cfg=cfg)
        embedder = LocalFastEmbedEmbedderFactory.build(cfg.embedding)
        chunker = StructuralChunkerFactory.build(cfg)
        att_filter = AttachmentFilter.from_lists(
            media_types=cfg.attachment_media_types,
            titles=cfg.attachment_titles,
        )
        conn = ConfluenceConnection(
            profile=cfg.confluence,
            body_format=cfg.body_format,
        )
        return await ConfluenceIngest.run(
            request_source=request_source,
            conn=conn,
            chunk_store=chunk_store,
            collections_store=collections_store,
            embedder=embedder,
            chunker=chunker,
            collection=cfg.collection,
            prune_missing=prune_missing,
            force_update=force_update,
            attachment_filter=att_filter,
            routes=routes,
        )
