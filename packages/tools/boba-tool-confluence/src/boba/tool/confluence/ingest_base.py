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
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ParseGrade,
)
from boba.confluence.rest import (
    CflPaginator,
    CflRestBuilder,
    ConfluenceConnection,
)
from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.pgvector.store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresSourceLedger,
)
from boba.doc.config import DocSection
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
    UnseenGone,
)
from boba.indexing.ports import Chunker, Embedder, ReaderId
from boba.indexing.values import CollectionId
from boba.llm.embedding import EmbeddingConfig
from boba.llm.warm import WarmEmbedder
from boba.tool.confluence.chunking import (
    ChunkerParams,
    StructuralChunkerFactory,
)
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
    ConfluenceDiscovery,
    ContentListing,
    CqlListing,
    PageListing,
    SpaceListing,
)
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import StringList
from boba.transport.http.profile import HttpConnection

__all__ = [
    "ConfluenceIngest",
    "ConfluenceIngestConfig",
    "IngestAssembly",
    "IngestLine",
    "IngestReport",
    "IngestReporting",
    "IngestScope",
    "IngestStamp",
    "PageScope",
    "QueryScope",
    "SpaceScope",
    "WideThreadPool",
]

logger = logging.getLogger("boba.tool.confluence.ingest")


class ConfluenceIngestConfig(PostgresStoreConfig, ChunkerParams, DocSection):
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
    page_workers: int = Field(
        ge=1,
        description=(
            "Сколько источников индексируется одновременно; обязателен. Каждый "
            "занимает поток разбора, поэтому реальный потолок задают лимиты "
            "песочницы: cpu-квота (`cgroup_cpu_percent`), суммарное cpu-время "
            "(`max_cpu_sec` — оно тратится в page_workers раз быстрее) и память "
            "(`cgroup_memory_bytes`: сессии OCR держат сотни MiB). Пул "
            "соединений postgres должен быть не меньше page_workers."
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
        """Секция под режим OCR вызова; язык и модели — из конфига."""
        return self.for_call(ocr=ocr)


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

    collection: str
    pages: IngestLine
    attachments: IngestLine

    def rows(self) -> Sequence[Mapping[str, Any]]:
        return [self.pages.model_dump(), self.attachments.model_dump()]

    def note(self) -> str:
        return f"collection: {self.collection}"


class IngestReporting:
    """Сборка отчёта прогона из итога конвейера и счётчиков прогресса:
    страницы и вложения отдельными строками, у сорвавшихся первая причина."""

    ERROR_CHARS: ClassVar[int] = 300

    def __init__(self, progress: IngestProgress, collection: str) -> None:
        self._progress = progress
        self._collection = collection

    def report(self, outcome: RunOutcome) -> IngestReport:
        return IngestReport(
            collection=self._collection,
            pages=self._line(
                "pages", outcome, SourceKind.ROOT, found=self._progress.found_pages()
            ),
            attachments=self._line(
                "attachments",
                outcome,
                SourceKind.CHILD,
                found=self._progress.found_attachments(),
            ),
        )

    def _reasons(self, by_reason: Mapping[str, int]) -> str:
        """Почему источники не пошли в индекс: причина и сколько раз."""
        parts: list[str] = []
        for reason, count in sorted(by_reason.items()):
            parts.append(f"{reason}: {count}")

        return ", ".join(parts)

    def _line(
        self, kind: str, outcome: RunOutcome, source_kind: SourceKind, *, found: int
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
            skipped_reasons=self._reasons(outcome.skips_of(source_kind)),
            error=outcome.reason_of(source_kind)[: self.ERROR_CHARS],
        )


class IngestStamp:
    """Штамп конвейера для реестра: модель, размерность, нарезка, ридеры."""

    SEPARATOR: ClassVar[str] = "|"

    def __init__(
        self, cfg: ConfluenceIngestConfig, routes: Mapping[str, Reader[str]]
    ) -> None:
        self._cfg = cfg
        self._routes = routes

    def render(self) -> str:
        reader_ids: set[str] = set()
        for reader in self._routes.values():
            reader_ids.add(str(reader.reader_id()))

        parts = [
            self._cfg.embedding.model,
            str(self._cfg.embedding.dim),
            str(self._cfg.chunk_size),
            str(self._cfg.chunk_overlap),
            ",".join(sorted(reader_ids)),
        ]

        return self.SEPARATOR.join(parts)


class IngestScope:
    """Что обходит прогон и что он вправе снимать за пределами увиденного.

    Спейс покрывает себя целиком: его страницы приходят списком контента, и
    страница, которой в списке нет, снимается с индекса. Запрос и одна
    страница области не имеют: выборка по CQL молчит о том, что в неё не
    попало, поэтому чужие страницы такой прогон не трогает. Вложения увиденных
    страниц снимаются в любом режиме — их полный список приходит вместе со
    страницей.

    Область записывается в реестр, поэтому спейс не удалит чужое даже когда
    прогоны идут одновременно.
    """

    SPACE_PREFIX: ClassVar[str] = "space:"

    def __init__(self, *, listing: ContentListing, space_key: str = "") -> None:
        self.listing = listing
        self.space_key = space_key
        self._rest = CflRestBuilder()

    def label(self) -> str:
        return self.listing.label()

    def owned(self) -> str:
        """Метка области для реестра; пустая — прогон корней не снимает."""
        if not self.space_key:
            return ""

        return self.SPACE_PREFIX + self.space_key

    def probe(self) -> SourceProbe:
        """Кому верить в вопросе «страница исчезла».

        Список контента спейса полон, поэтому для спейса ответ даёт сам обход.
        У запроса и одной страницы области нет, снимать им нечего.
        """
        if not self.space_key:
            return NoProbe()

        return UnseenGone()

    async def verify(self, paginator: CflPaginator) -> None:
        """Спейс должен существовать; остальные режимы проверять нечем."""
        if not self.space_key:
            return

        await paginator.get_json(self._rest.space_path(self.space_key))


class SpaceScope(IngestScope):
    """Область одного спейса: список контента полон, чужое снимается."""

    def __init__(self, space_key: str) -> None:
        super().__init__(listing=SpaceListing(space_key), space_key=space_key)


class QueryScope(IngestScope):
    """Область CQL-запроса: без права снимать невиденное."""

    def __init__(self, cql: str) -> None:
        super().__init__(listing=CqlListing(cql))


class PageScope(IngestScope):
    """Область одной страницы: без права снимать невиденное."""

    def __init__(self, page_id: str) -> None:
        super().__init__(listing=PageListing(page_id))


class WideThreadPool:
    """Свой пул под asyncio.to_thread на время прогона: дефолтный ограничен
    min(32, cpu+4).

    Слотов на один больше числа источников — разбор не должен ждать, пока
    освободится поток, занятый эмбеддингом батча. На выходе широкий пул
    закрывается (брошенный, он остался бы дефолтным на весь процесс, и
    завершение цикла ждало бы его потоки), но дефолтным сначала становится
    свежий: закрытый executor валит любой следующий to_thread с «cannot
    schedule new futures after shutdown» — например kerberos-логин соединения
    postgres.
    """

    THREAD_PREFIX: ClassVar[str] = "boba-ingest"

    def __init__(self, workers: int) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=workers + 1, thread_name_prefix=self.THREAD_PREFIX
        )

    async def __aenter__(self) -> Self:
        asyncio.get_running_loop().set_default_executor(self._pool)

        return self

    async def __aexit__(self, *exc: object) -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor())
        self._pool.shutdown(wait=False, cancel_futures=True)


class ConfluenceIngest:
    """Конвейер Confluence -> kb_chunks над собранными стадиями: общий хвост
    для confluence_index_*.

    Стадии (хранилища, реестр, эмбеддер, чанкер, гейт вложений, ридеры)
    приходят в конструктор; здесь из них собираются транспорт, обход
    источников, диспетчер ридеров и отчёт. Стенд собирает стадии сам,
    инструменты — через IngestAssembly.
    """

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения от ConfluenceJsonDecoder, уходящие в HTML-Reader."""

    DISPATCH_READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence_dispatch")

    def __init__(  # noqa: PLR0913 — стадии конвейера независимы
        self,
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
    ) -> None:
        self._scope = scope
        self._collections_store = collections_store
        self._ledger = ledger
        self._chunker = chunker
        self._workers = workers
        self._collection_id = CollectionId(collection)
        self._paginator = CflPaginator(conn)
        self._reader: DispatchReader[str] = DispatchReader(
            by=TransportKeys.CONTENT_TYPE,
            routes=dict(routes),
            reader_id=self.DISPATCH_READER_ID,
            on_unknown="skip",
        )
        self._view: CollectionScopedView[str] = CollectionScopedView(
            store=chunk_store, embedder=embedder, collection=self._collection_id
        )
        self._transport = ConfluenceSourceTransport(conn)
        self._source = ConfluenceDiscovery(
            conn=conn,
            listing=scope.listing,
            gate=gate,
            grade=grade,
            progress=progress,
        )
        self._config: IndexerConfig[str] = IndexerConfig(
            workers=workers, stamp=stamp, scope=scope.owned()
        )
        self._run_log = LoggedIndexRun(logger, progress)
        self._reporting = IngestReporting(progress, str(self._collection_id))

    async def run(self) -> IngestReport:
        """Полный Confluence -> kb_chunks конвейер для собранной области."""
        async with self._paginator as paginator:
            await self._scope.verify(paginator)

        logger.info("db ensure_collection start: %s", self._collection_id)
        elapsed = Elapsed()
        await self._collections_store.ensure_collection(
            self._collection_id, description=None
        )
        logger.info(
            "db ensure_collection done: %s in %dms", self._collection_id, elapsed.ms()
        )

        async with WideThreadPool(self._workers):
            try:
                pipeline: Pipeline[Any, str] = Pipeline(
                    source=self._source,
                    transport=self._transport,
                    reader=self._reader,
                    ledger=self._ledger,
                    probe=self._scope.probe(),
                )
                events = pipeline.index(
                    chunker=self._chunker, sink=self._view, config=self._config
                )
                outcome = await self._run_log.drain(events)
            finally:
                await self._transport.close()

        return self._reporting.report(outcome)


class IngestAssembly:
    """Сборка стадий конвейера из секции инструмента: хранилища, реестр,
    эмбеддер, чанкер и гейт вложений живут здесь, конвейер собирается на
    область вызова."""

    def __init__(
        self,
        cfg: ConfluenceIngestConfig,
        progress: IngestProgress,
        routes: Mapping[str, Reader[str]],
    ) -> None:
        self._cfg = cfg
        self._progress = progress
        self._routes = routes
        self._chunk_store = LoggingChunkStore(PostgresChunkStore(cfg=cfg), logger)
        self._collections_store = PostgresCollectionsStore(cfg=cfg)
        self._ledger = LoggingSourceLedger(
            PostgresSourceLedger(cfg=cfg, collection=CollectionId(cfg.collection)),
            logger,
        )
        self._embedder = LoggingEmbedder(WarmEmbedder.of(cfg.embedding), logger)
        self._chunker = LoggingChunker(
            StructuralChunkerFactory(cfg).build(), logger, progress
        )
        self._stamp = IngestStamp(cfg, routes)
        self._conn = ConfluenceConnection(
            profile=cfg.confluence, body_format=cfg.body_format
        )

    def build(self, scope: IngestScope, *, attachments: bool) -> ConfluenceIngest:
        cfg = self._cfg
        gate = AttachmentGate(
            allowed=AttachmentFilter(cfg.attachments),
            requested=attachments,
            ocr=cfg.ocr.enabled,
        )
        grade = ParseGrade.TEXT
        if cfg.ocr.enabled:
            grade = ParseGrade.OCR

        logger.info(
            "ingest %s: attachments=%s ocr=%s",
            scope.label(),
            attachments,
            cfg.ocr.enabled,
        )

        return ConfluenceIngest(
            scope=scope,
            conn=self._conn,
            chunk_store=self._chunk_store,
            collections_store=self._collections_store,
            ledger=self._ledger,
            embedder=self._embedder,
            chunker=self._chunker,
            collection=cfg.collection,
            workers=cfg.page_workers,
            stamp=self._stamp.render(),
            progress=self._progress,
            gate=gate,
            grade=grade,
            routes=self._routes,
        )
