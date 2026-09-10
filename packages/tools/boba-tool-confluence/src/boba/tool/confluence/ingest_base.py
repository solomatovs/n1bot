"""Общая база Confluence-ingest: конфиг и сборка конвейера для confluence_index_*.

Ошибки:
LedgerError — реестр источников недоступен, прогон оборван.
PostgresError — до хранилища чанков не достучаться.
EmbeddingError — эмбеддер недоступен или ответил мусором.
TransportError — список страниц забрать не удалось; отказ отдельной страницы
    или вложения наружу не выходит, он считается в failed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ParseGrade,
)
from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.pgvector.store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresSourceLedger,
)
from boba.indexing import (
    ChunkStore,
    CollectionScopedView,
    DispatchReader,
    IndexerConfig,
    NoProbe,
    Pipeline,
    Reader,
    SourceKind,
    SourceLedger,
    SourceProbe,
    TransportKeys,
)
from boba.indexing.ports import Chunker, Embedder, ReaderId
from boba.indexing.values import CollectionId
from boba.llm.embedding import EmbeddingConfig
from boba.llm.warm import WarmEmbedder
from boba.text.document import LiteParseParams
from boba.tool.confluence.chunking import (
    ChunkerParams,
    StructuralChunkerFactory,
)
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.indexing_log import (
    IngestProgress,
    LoggedIndexRun,
    LoggingChunker,
    LoggingChunkStore,
    LoggingEmbedder,
    LoggingSourceLedger,
    RunOutcome,
)
from boba.tool.confluence.pipeline import ConfluenceSourceTransport
from boba.tool.confluence.request_sources import (
    ConfluenceCql,
    ConfluenceDiscovery,
    ConfluencePaginator,
    ConfluenceProbe,
    ConfluenceRest,
)
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import StringList
from boba.transport.http.profile import HttpConnection

__all__ = [
    "ConfluenceIngest",
    "ConfluenceIngestConfig",
    "IngestLine",
    "IngestReport",
    "IngestScope",
    "IngestStamp",
]

logger = logging.getLogger("boba.tool.confluence.ingest")


class ConfluenceIngestConfig(PostgresStoreConfig, ChunkerParams, LiteParseParams):
    """Self-contained конфиг семейства tool'ов confluence_index_*."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingConfig
    confluence: HttpConnection
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
    attachments: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist вложений масками fnmatch: маска с косой чертой — "
                "тип содержимого (`application/pdf`, `image/*`), без неё — "
                "имя файла (`*.pdf`). Пусто — разрешены все вложения. "
                "Вызов с attachments=true берёт всё, что проходит allowlist."
            ),
        ),
    ]
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
    ]
    page_workers: int = Field(
        ge=1,
        description=(
            "Сколько источников индексируется одновременно; обязателен. Каждый "
            "занимает поток разбора, поэтому реальный потолок задают лимиты "
            "песочницы: cpu-квота (`cgroup_cpu_percent`), суммарное cpu-время "
            "(`max_cpu_sec` — оно тратится в page_workers раз быстрее) и память "
            "(`cgroup_memory_bytes`: OCR берёт `num_workers` × 50-100 MiB на "
            "документ). Пул соединений postgres должен быть не меньше page_workers."
        ),
    )

    @model_validator(mode="after")
    def _pool_fits_workers(self) -> Self:
        """Параллельные источники пишут одновременно — пула должно хватать."""
        pool = self.connection.pool
        limit = pool.max_size if pool.max_size is not None else pool.min_size
        if limit < self.page_workers:
            msg = (
                f"confluence ingest config: page_workers={self.page_workers} "
                f"exceeds the postgres connection pool ({limit}), "
                f"raise connection.pool.max_size"
            )
            raise ValueError(msg)
        return self

    def with_ocr(self, *, ocr: bool) -> Self:
        """Копия с режимом OCR, выбранным вызовом; язык и воркеры из конфига."""
        return self.model_copy(update={"ocr_enabled": ocr})


class IngestLine(BaseModel):
    """Строка отчёта по одному виду источников."""

    kind: str
    found: int
    indexed: int
    unchanged: int
    skipped: int
    failed: int
    deleted: int
    chunks: int
    chunks_deleted: int
    skipped_reasons: str
    error: str


class IngestReport(BaseModel):
    """Итог прогона глазами вызывающего: страницы и вложения отдельными строками.

    Одного числа записанных чанков мало: ноль означает и «нечего было менять»,
    и «ничего не доехало». Поэтому в строке видно, сколько источников нашлось
    у Confluence, сколько совпало с индексом, сколько отсечено правилами и
    сколько сорвалось, а у сорвавшихся показана первая причина.
    """

    ERROR_CHARS: ClassVar[int] = 300

    collection: str
    pages: IngestLine
    attachments: IngestLine

    @classmethod
    def of(
        cls, outcome: RunOutcome, progress: IngestProgress, *, collection: str
    ) -> IngestReport:
        return cls(
            collection=collection,
            pages=cls._line(
                "pages", outcome, SourceKind.ROOT, found=progress.found_pages()
            ),
            attachments=cls._line(
                "attachments",
                outcome,
                SourceKind.CHILD,
                found=progress.found_attachments(),
            ),
        )

    @staticmethod
    def _reasons(by_reason: Mapping[str, int]) -> str:
        """Почему источники не пошли в индекс: причина и сколько раз."""
        parts: list[str] = []
        for reason, count in sorted(by_reason.items()):
            parts.append(f"{reason}: {count}")

        return ", ".join(parts)

    @classmethod
    def _line(
        cls,
        kind: str,
        outcome: RunOutcome,
        source_kind: SourceKind,
        *,
        found: int,
    ) -> IngestLine:
        tally = outcome.stats.tally_of(source_kind)
        return IngestLine(
            kind=kind,
            found=found,
            indexed=tally.indexed,
            unchanged=tally.unchanged,
            skipped=tally.skipped,
            failed=tally.failed,
            deleted=tally.deleted,
            chunks=tally.chunks_upserted,
            chunks_deleted=tally.chunks_deleted,
            skipped_reasons=cls._reasons(outcome.skips_of(source_kind)),
            error=outcome.reason_of(source_kind)[: cls.ERROR_CHARS],
        )

    def rows(self) -> Sequence[Mapping[str, Any]]:
        return [self.pages.model_dump(), self.attachments.model_dump()]

    def note(self) -> str:
        return f"collection: {self.collection}"


class IngestStamp:
    """Штамп конвейера для реестра: модель, размерность, нарезка, ридеры."""

    SEPARATOR: ClassVar[str] = "|"

    @classmethod
    def of(cls, cfg: ConfluenceIngestConfig, routes: Mapping[str, Reader[str]]) -> str:
        reader_ids: set[str] = set()
        for reader in routes.values():
            reader_ids.add(str(reader.reader_id()))

        parts = [
            cfg.embedding.model,
            str(cfg.embedding.dim),
            str(cfg.chunk_size),
            str(cfg.chunk_overlap),
            ",".join(sorted(reader_ids)),
        ]
        return cls.SEPARATOR.join(parts)


class IngestScope:
    """Что обходит прогон и что он вправе снимать за пределами увиденного.

    Спейс покрывает себя целиком: страницы, которых он больше не отдаёт,
    проверяются пробой и снимаются. Запрос и одна страница области не имеют:
    выборка по CQL молчит о том, что в неё не попало, поэтому чужие страницы
    такой прогон не трогает. Вложения увиденных страниц снимаются в любом
    режиме — их полный список приходит вместе со страницей.

    Область записывается в реестр, поэтому спейс не удалит чужое даже когда
    прогоны идут одновременно.
    """

    SPACE_PREFIX: ClassVar[str] = "space:"

    def __init__(self, *, cql: str, space_key: str = "") -> None:
        self.cql = cql
        self.space_key = space_key

    @classmethod
    def space(cls, space_key: str) -> IngestScope:
        return cls(cql=ConfluenceCql.space(space_key), space_key=space_key)

    @classmethod
    def query(cls, cql: str) -> IngestScope:
        return cls(cql=cql)

    @classmethod
    def page(cls, page_id: str) -> IngestScope:
        return cls(cql=ConfluenceCql.page(page_id))

    def owned(self) -> str:
        """Метка области для реестра; пустая — прогон корней не снимает."""
        if not self.space_key:
            return ""

        return self.SPACE_PREFIX + self.space_key

    def probe_of(self, conn: ConfluenceConnection) -> SourceProbe:
        if not self.space_key:
            return NoProbe()

        return ConfluenceProbe(conn)

    async def verify(self, conn: ConfluenceConnection) -> None:
        """Спейс должен существовать; остальные режимы проверять нечем."""
        if not self.space_key:
            return

        async with ConfluencePaginator(conn) as paginator:
            await paginator.get_json(ConfluenceRest.space_path(self.space_key))


class ConfluenceIngest:
    """Сборка Confluence-ingest конвейера — общий хвост для confluence_index_*."""

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения от ConfluenceJsonDecoder, уходящие в HTML-Reader."""

    @staticmethod
    async def run(  # noqa: PLR0913 — стадии конвейера независимы
        *,
        scope: IngestScope,
        conn: ConfluenceConnection,
        chunk_store: ChunkStore[str],
        collections_store: PostgresCollectionsStore,
        ledger: SourceLedger,
        embedder: Embedder[str],
        chunker: Chunker[str],
        collection: str,
        workers: int,
        stamp: str,
        progress: IngestProgress,
        gate: AttachmentGate,
        grade: ParseGrade,
        routes: Mapping[str, Reader[str]],
    ) -> IngestReport:
        """Полный Confluence -> kb_chunks конвейер для собранного scope."""
        await scope.verify(conn)

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
        transport = ConfluenceSourceTransport.from_connection(conn)
        source = ConfluenceDiscovery(
            conn=conn,
            cql=scope.cql,
            gate=gate,
            grade=grade,
            progress=progress,
        )
        config: IndexerConfig[str] = IndexerConfig(
            workers=workers, stamp=stamp, scope=scope.owned()
        )
        async with ConfluenceIngest._wide_thread_pool(workers):
            try:
                pipeline: Pipeline[Any, str] = Pipeline(
                    source=source,
                    transport=transport,
                    reader=reader,
                    ledger=ledger,
                    probe=scope.probe_of(conn),
                )
                outcome = await LoggedIndexRun.drain(
                    pipeline.index(chunker=chunker, sink=view, config=config),
                    logger,
                    progress,
                )
            finally:
                await transport.close()

        return IngestReport.of(outcome, progress, collection=str(collection_id))

    @staticmethod
    @asynccontextmanager
    async def _wide_thread_pool(workers: int) -> AsyncGenerator[None, None]:
        """Свой пул под asyncio.to_thread: дефолтный ограничен min(32, cpu+4).

        Слотов на один больше числа источников — разбор не должен ждать, пока
        освободится поток, занятый эмбеддингом батча. На выходе пул закрывается:
        подменённый и брошенный, он остаётся дефолтным на весь процесс, и
        завершение asyncio.run ждёт его потоки.
        """
        pool = ThreadPoolExecutor(
            max_workers=workers + 1,
            thread_name_prefix="boba-ingest",
        )
        asyncio.get_running_loop().set_default_executor(pool)

        try:
            yield
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    async def ingest(
        cfg: ConfluenceIngestConfig,
        scope: IngestScope,
        *,
        attachments: bool,
        progress: IngestProgress,
        routes: Mapping[str, Reader[str]],
    ) -> IngestReport:
        """Собрать stores/ledger/embedder/chunker/gate из cfg и вызвать run."""
        chunk_store = LoggingChunkStore(PostgresChunkStore(cfg=cfg), logger)
        collections_store = PostgresCollectionsStore(cfg=cfg)
        ledger = LoggingSourceLedger(
            PostgresSourceLedger(cfg=cfg, collection=CollectionId(cfg.collection)),
            logger,
        )
        embedder = LoggingEmbedder(WarmEmbedder.of(cfg.embedding), logger)
        chunker = LoggingChunker(StructuralChunkerFactory.build(cfg), logger, progress)
        gate = AttachmentGate(
            allowed=AttachmentFilter.of_masks(cfg.attachments),
            requested=attachments,
            ocr=cfg.ocr_enabled,
        )
        grade = ParseGrade.of(ocr=cfg.ocr_enabled)
        logger.info(
            "ingest %s: attachments=%s ocr=%s", scope.cql, attachments, cfg.ocr_enabled
        )
        conn = ConfluenceConnection(
            profile=cfg.confluence,
            body_format=cfg.body_format,
        )
        return await ConfluenceIngest.run(
            scope=scope,
            conn=conn,
            chunk_store=chunk_store,
            collections_store=collections_store,
            ledger=ledger,
            embedder=embedder,
            chunker=chunker,
            collection=cfg.collection,
            workers=cfg.page_workers,
            stamp=IngestStamp.of(cfg, routes),
            progress=progress,
            gate=gate,
            grade=grade,
            routes=routes,
        )
