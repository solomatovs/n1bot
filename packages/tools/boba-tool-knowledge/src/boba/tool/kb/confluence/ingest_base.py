"""Общая база Confluence-ingest: конфиг и сборка pipeline'а для confluence_ingest_*."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.pgvector.store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.indexing import (
    ChunkStore,
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
from boba.indexing.ports import Chunker, Embedder, ReaderId
from boba.indexing.values import CollectionId
from boba.text.document import LiteParseParams
from boba.tool.kb.chunking import (
    ChunkerParams,
    StructuralChunkerFactory,
)
from boba.tool.kb.confluence.cleanup import ConfluencePageScopeCleanup
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter, AttachmentGate
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.request_sources import ConfluenceRequest
from boba.tool.kb.indexing_log import (
    IngestProgress,
    LoggedIndexRun,
    LoggingChunker,
    LoggingChunkStore,
    LoggingEmbedder,
)
from boba.tool.kb.warm import EmbeddingConfig, WarmEmbedder
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import StringList
from boba.transport.http.profile import HttpProfile

__all__ = ["ConfluenceIngest", "ConfluenceIngestConfig"]

logger = logging.getLogger("boba.tool.kb.confluence.ingest")


class ConfluenceIngestConfig(PostgresStoreConfig, ChunkerParams, LiteParseParams):
    """Self-contained конфиг семейства tool'ов confluence_ingest_*."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingConfig
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
    text_encodings: Annotated[
        StringList,
        Field(
            min_length=1,
            description=(
                "Кодировки текстовых вложений (`text/plain`, `text/markdown`, "
                "`text/csv`) в порядке перебора: первая, которой payload "
                "декодировался, и выигрывает. Не подошла ни одна — вложение "
                "уходит в `failed`."
            ),
        ),
    ] = ["utf-8"]  # noqa: RUF012
    page_workers: int = Field(
        ge=1,
        description=(
            "Сколько страниц индексируется одновременно; обязателен. Каждая "
            "страница занимает поток разбора, поэтому реальный потолок задают "
            "лимиты песочницы: cpu-квота (`cgroup_cpu_percent`), суммарное "
            "cpu-время (`max_cpu_sec` — оно тратится в page_workers раз "
            "быстрее) и память (`cgroup_memory_bytes`: вложение читается "
            "целиком, а OCR берёт ещё `num_workers` × 50-100 MiB на страницу). "
            "Пул соединений postgres должен быть не меньше page_workers."
        ),
    )

    @model_validator(mode="after")
    def _pool_fits_workers(self) -> Self:
        """Параллельные страницы пишут одновременно — пула должно хватать."""
        pool = self.connection.pool
        limit = pool.max_size if pool.max_size is not None else pool.min_size
        if limit < self.page_workers:
            msg = (
                f"page_workers={self.page_workers} превышает пул соединений "
                f"postgres ({limit}): подними connection.pool.max_size"
            )
            raise ValueError(msg)
        return self


class ConfluenceIngest:
    """Сборка Confluence-ingest pipeline — общий хвост для confluence_ingest_*."""

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения от ConfluenceJsonDecoder, уходящие в HTML-Reader."""

    @staticmethod
    async def run(  # noqa: PLR0913
        *,
        request_source: RequestSource[ConfluenceRequest],
        conn: ConfluenceConnection,
        chunk_store: ChunkStore[str],
        collections_store: PostgresCollectionsStore,
        embedder: Embedder[str],
        chunker: Chunker[str],
        collection: str,
        prune_missing: bool,
        workers: int,
        progress: IngestProgress,
        force_update: bool = False,
        gate: AttachmentGate,
        routes: Mapping[str, Reader[str]],
        skip_failed: bool,
    ) -> dict[str, Any]:
        """Полный Confluence -> kb_chunks pipeline для уже собранного RequestSource."""
        reader: DispatchReader[str] = DispatchReader(
            by=TransportKeys.CONTENT_TYPE,
            routes=dict(routes),
            reader_id=ReaderId("ext.confluence_dispatch"),
            on_unknown="skip",
        )

        collection_id = CollectionId(collection)
        logger.info("db ensure_collection start: %s", collection_id)
        elapsed = Elapsed()
        await collections_store.ensure_collection(collection_id, description=None)
        logger.info(
            "db ensure_collection done: %s in %dms", collection_id, elapsed.ms()
        )

        view: CollectionScopedView[str] = CollectionScopedView(
            store=chunk_store,
            embedder=embedder,
            collection=collection_id,
        )
        transport = ConfluenceContentTransport.from_connection(
            conn,
            progress=progress,
            gate=gate,
            skip_failed=skip_failed,
        )

        # prune_missing сносит весь стейл коллекции; force_update без prune — страницы
        if prune_missing:
            cleanup: CleanupStrategy = FullCleanup()
        elif force_update:
            cleanup = ConfluencePageScopeCleanup()
        else:
            cleanup = NoneCleanup()

        config: IndexerConfig[str] = IndexerConfig(
            cleanup=cleanup,
            force_update=force_update,
            workers=workers,
            skip_failed=skip_failed,
        )
        ConfluenceIngest._widen_thread_pool(workers)
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
                progress,
            )
        finally:
            await transport.close()

        return {
            "collection": str(collection_id),
            "indexed": stats.chunks_upserted,
            "skipped_unchanged": stats.sources_skipped_unchanged,
            "pruned": stats.chunks_deleted,
            "failed": stats.sources_failed,
        }

    @staticmethod
    def _widen_thread_pool(workers: int) -> None:
        """Свой пул под asyncio.to_thread: дефолтный ограничен min(32, cpu+4).

        Слотов на один больше числа страниц — разбор не должен ждать, пока
        освободится поток, занятый эмбеддингом батча.
        """
        pool = ThreadPoolExecutor(
            max_workers=workers + 1,
            thread_name_prefix="boba-ingest",
        )
        asyncio.get_running_loop().set_default_executor(pool)

    @staticmethod
    async def ingest(  # noqa: PLR0913 — режимы обхода и наблюдение независимы
        cfg: ConfluenceIngestConfig,
        request_source: RequestSource[ConfluenceRequest],
        prune_missing: bool,
        force_update: bool = False,
        *,
        attachments: str,
        progress: IngestProgress,
        routes: Mapping[str, Reader[str]],
        skip_failed: bool,
    ) -> dict[str, Any]:
        """Собрать stores/embedder/chunker/filter из cfg и вызвать run."""
        chunk_store = LoggingChunkStore(PostgresChunkStore(cfg=cfg), logger)
        collections_store = PostgresCollectionsStore(cfg=cfg)
        embedder = LoggingEmbedder(WarmEmbedder.of(cfg.embedding), logger)
        chunker = LoggingChunker(StructuralChunkerFactory.build(cfg), logger, progress)
        allowed = AttachmentFilter.from_lists(
            media_types=cfg.attachment_media_types,
            titles=cfg.attachment_titles,
        )
        gate = AttachmentGate.of(allowed, attachments, ocr_enabled=cfg.ocr_enabled)
        logger.info("attachments requested: %r, ocr=%s", attachments, cfg.ocr_enabled)
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
            workers=cfg.page_workers,
            progress=progress,
            force_update=force_update,
            gate=gate,
            routes=routes,
            skip_failed=skip_failed,
        )
