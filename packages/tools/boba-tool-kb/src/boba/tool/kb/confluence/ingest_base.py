"""Общая база Confluence-ingest: `ConfluenceIngestConfig` + `ConfluenceIngest`.

Сами tool'ы физически разнесены по файлам (по одному на режим discovery) —
`ingest_spaces.py` / `ingest_pages.py` / `ingest_cql.py`. Здесь — то, что у них
пока общее: конфиг-секция `[tool.kb.confluence.ingest]` и сборка pipeline'а.

Каждый tool-файл — самостоятельная точка дивергенции: когда режим начнёт
собирать таблицу/коллекцию иначе, он форкает свой кусок (свой `RequestSource`
уже там; при необходимости — свой `run`/конфиг), не задевая остальные.

Pipeline: `RequestSource` → `ConfluenceContentTransport` (HTTP + JSON-decode +
attachment fan-out) → `DispatchReader` по `CONTENT_TYPE` (HTML →
`ConfluenceReader`, прочее → skip) → `StructuralChunker` →
`CollectionScopedView` → `PostgresChunkStore`. Вложения (PDF/docx, прошедшие
`AttachmentFilter`) индексируются как отдельные чанки с `source_id` = URL
вложения и теми же `confluence.*` ключами родительской страницы. Набор
metadata-ключей — `boba.tool.kb.confluence.models`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, ClassVar

from pydantic import Field

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
from boba.settings import (
    BobaFlatSettings,
    BobaSettingsConfigDict,
    StringList,
)
from boba.text import StructuralChunker
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.reading import ConfluenceReader
from boba.tool.kb.core.chunking import ChunkerParams
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.indexing_log import LoggedIndexRun
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.transport.http import HttpRequest

__all__ = ["ConfluenceIngest", "ConfluenceIngestConfig"]

logger = logging.getLogger("boba.tool.kb.confluence.ingest")


class ConfluenceIngestConfig(BobaFlatSettings):
    """Self-contained конфиг семейства tool'ов `confluence_ingest_*`.

    Config-секция: `[tool.kb.confluence.ingest]`. Пока делится между всеми
    тремя режимами (spaces / pages / cql); при дивергенции режим заводит свою.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.ingest",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
            "confluence",
        ),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    confluence: ConfluenceConnection
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
    """Сборка Confluence-ingest pipeline — общий хвост для `confluence_ingest_*`."""

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения, которые `ConfluenceJsonDecoder` ставит после
    распаковки JSON→HTML; всё, что попадает под эти ключи, идёт в HTML-Reader."""

    PIPELINE_ID_SPACES: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_spaces")
    PIPELINE_ID_PAGES: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_pages")
    PIPELINE_ID_CQL: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_cql")

    @staticmethod
    def run(  # noqa: PLR0913 — keyword-only helper, явный набор deps
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
            routes=dict.fromkeys(
                ConfluenceIngest.HTML_CONTENT_TYPES, confluence_reader,
            ),
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
            transport=ConfluenceContentTransport.from_connection(
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
        stats = LoggedIndexRun.invoke(
            indexer, PipelineContext(pipeline_id=pipeline_id), config, logger,
        )

        return {
            "collection": str(collection_id),
            "indexed": stats.chunks_upserted,
            "skipped_unchanged": stats.sources_skipped_unchanged,
            "pruned": stats.chunks_deleted,
            "failed": stats.sources_failed,
        }

    @staticmethod
    def ingest(
        cfg: ConfluenceIngestConfig,
        request_source: RequestSource[HttpRequest],
        prune_missing: bool,
        pipeline_id: PipelineId,
    ) -> dict[str, Any]:
        """Собрать stores/embedder/chunker/filter из cfg и вызвать `run`."""
        chunk_store = PostgresChunkStore(cfg=cfg.store)
        collections_store = PostgresCollectionsStore(cfg=cfg.store)
        embedder = cfg.embedding.build()
        chunker = cfg.chunker.build_chunker()
        att_filter = AttachmentFilter.from_lists(
            media_types=cfg.attachment_media_types,
            titles=cfg.attachment_titles,
        )
        return ConfluenceIngest.run(
            request_source=request_source,
            conn=cfg.confluence,
            chunk_store=chunk_store,
            collections_store=collections_store,
            embedder=embedder,
            chunker=chunker,
            collection=cfg.collection,
            prune_missing=prune_missing,
            pipeline_id=pipeline_id,
            attachment_filter=att_filter,
        )
