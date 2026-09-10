"""Реестр источников на живом стенде: повтор не качает, удалённое уходит.

Настоящие: конвейер, транспорт Confluence, discovery, реестр и хранилище чанков
в postgres на схеме стенда (боевые миграции). Заглушкой служит только сам
Confluence (uvicorn), эмбеддер отдаёт нулевые векторы.

pytest -m integration.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from confluence_stand import (
    ConfluenceStub,
    LiveServer,
    StubAttachment,
    StubPage,
    StubRoute,
)
from ingest_stand import TextReader, ZeroEmbedder
from omegaconf import DictConfig
from psycopg import AsyncConnection, sql

from boba.config import bind
from boba.db.pgvector.config import PostgresStoreConfig, PostgresStoreSchema
from boba.db.pgvector.migrations import Migrations
from boba.db.pgvector.store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresSourceLedger,
)
from boba.db.postgres import AsyncPostgresPool
from boba.indexing import (
    CollectionId,
    RawDocument,
    Reader,
    ReaderId,
    Section,
    SourceId,
    SourceRecord,
)
from boba.tool.kb.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import (
    ConfluenceIngest,
    ConfluenceIngestConfig,
    IngestScope,
)
from boba.tool.kb.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ConfluenceSourceId,
    ParseGrade,
)
from boba.tool.kb.confluence.request_sources import ConfluenceRest
from boba.tool.kb.indexing_log import (
    IngestProgress,
    LoggingChunker,
    LoggingChunkStore,
    LoggingSourceLedger,
)
from boba.transport.http.profile import HttpConnection, UrlScheme

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LOGGER = logging.getLogger("test.ingest.ledger")

SCHEMA = "kb_ingest_test"
COLLECTION = "kb_test"
SPACE = "DOCS"
DIM = ZeroEmbedder.DIM

PDF = "application/pdf"
TEXT = "text/plain"
PNG = "image/png"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class CountingReader(Reader[str]):
    """Одна секция на документ; считает, что реально дошло до разбора."""

    def __init__(self) -> None:
        self.reads: list[SourceId] = []

    async def read(self, value: RawDocument) -> AsyncIterator[Section[str]]:
        self.reads.append(value.source_id)
        async for section in TextReader().read(value):
            yield section

    def reader_id(self) -> ReaderId:
        return ReaderId("test.counting")

    def parsed(self, suffix: str) -> int:
        count = 0
        for source_id in self.reads:
            if str(source_id).endswith(suffix):
                count += 1

        return count


@pytest.fixture(scope="module")
async def store_cfg(raw_config: DictConfig) -> AsyncIterator[PostgresStoreConfig]:
    """Схема стенда под боевыми миграциями; сносится после модуля."""
    app_cfg = bind(raw_config, "tool.ingest", ConfluenceIngestConfig)
    tables = PostgresStoreSchema(
        pg_schema=SCHEMA,
        chunks_table="kb_chunks",
        collections_table="kb_collections",
        sources_table="kb_sources",
    )
    cfg = PostgresStoreConfig(connection=app_cfg.connection, tables=tables)

    pool = AsyncPostgresPool(cfg.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            await _execute(
                conn, sql.SQL("drop schema if exists {} cascade").format(_schema())
            )
            await _execute(conn, sql.SQL("create schema {}").format(_schema()))
            await Migrations.apply_bootstrap(conn, schema_cfg=tables)
            await Migrations.ensure_vector_index(conn, dim=DIM, schema_cfg=tables)

        yield cfg
    finally:
        async with pool.connection() as conn:
            await _execute(
                conn, sql.SQL("drop schema if exists {} cascade").format(_schema())
            )
        await pool.close()


def _schema() -> sql.Identifier:
    return sql.Identifier(SCHEMA)


def _drop_schema() -> sql.Composed:
    return sql.SQL("drop schema if exists {} cascade").format(_schema())


async def _execute(conn: AsyncConnection[Any], statement: sql.Composed) -> None:
    async with conn.transaction():
        await conn.execute(statement)


class IngestStand:
    """Прогон ingest против заглушки: наружу — итог, счётчики и реестр."""

    ATTACHMENT_MASKS: ClassVar[tuple[str, ...]] = (PDF, TEXT, PNG)

    def __init__(
        self, stub: ConfluenceStub, port: int, cfg: PostgresStoreConfig
    ) -> None:
        self.stub = stub
        self.cfg = cfg
        self.reader = CountingReader()
        self.progress = IngestProgress(LOGGER)
        self._port = port
        self.ledger = PostgresSourceLedger(cfg=cfg, collection=CollectionId(COLLECTION))
        self.chunks = PostgresChunkStore(cfg=cfg)

    def connection(self) -> ConfluenceConnection:
        profile = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=self._port,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            timeout_sec=10.0,
        )
        return ConfluenceConnection(profile=profile, body_format="view")

    def page_source(self, page_id: str) -> SourceId:
        path = ConfluenceRest.page_body_path(page_id, body_format="view")
        return ConfluenceSourceId.of(self.connection().profile, path)

    def attachment_source(self, page_id: str, title: str) -> SourceId:
        return ConfluenceSourceId.of(
            self.connection().profile, f"/download/attachments/{page_id}/{title}"
        )

    async def run(
        self,
        scope: IngestScope,
        *,
        attachments: bool = True,
        ocr: bool = False,
        workers: int = 2,
    ) -> dict[str, Any]:
        self.stub.reset_calls()
        self.reader.reads.clear()
        gate = AttachmentGate(
            allowed=AttachmentFilter.of_masks(self.ATTACHMENT_MASKS),
            requested=attachments,
            ocr=ocr,
        )
        routes: dict[str, Reader[str]] = {
            "text/html": self.reader,
            PDF: self.reader,
            TEXT: self.reader,
            PNG: self.reader,
        }
        params = ChunkerParams(chunk_size=200, chunk_overlap=0)
        return await ConfluenceIngest.run(
            scope=scope,
            conn=self.connection(),
            chunk_store=LoggingChunkStore(self.chunks, LOGGER),
            collections_store=PostgresCollectionsStore(cfg=self.cfg),
            ledger=LoggingSourceLedger(self.ledger, LOGGER),
            embedder=ZeroEmbedder(),
            chunker=LoggingChunker(
                StructuralChunkerFactory.build(params), LOGGER, self.progress
            ),
            collection=COLLECTION,
            workers=workers,
            stamp="test-stamp",
            progress=self.progress,
            gate=gate,
            grade=ParseGrade.of(ocr=ocr),
            routes=routes,
        )

    async def record(self, source_id: SourceId) -> SourceRecord | None:
        return await self.ledger.lookup(source_id)

    async def chunk_count(self, source_id: SourceId) -> int:
        pool = AsyncPostgresPool(self.cfg.connection)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    sql.SQL(
                        "select count(*) from {} "
                        "where collection = %s and source_id = %s"
                    ).format(self.cfg.tables.chunks_ident()),
                    (COLLECTION, str(source_id)),
                )
                row = await cur.fetchone()
        finally:
            await pool.close()

        if row is None:
            return 0

        return int(row[0])

    async def sources(self) -> Sequence[str]:
        ids: list[str] = []
        async for record in self.ledger.unseen(before=1e12):
            ids.append(str(record.source_id))

        return ids


def _space(stub: ConfluenceStub) -> None:
    """Три страницы: с двумя вложениями, с картинкой, пустая."""
    stub.add(
        StubPage(
            id="101",
            space=SPACE,
            title="Overview",
            html="<h1>Overview</h1><p>alpha text</p>",
            attachments=[
                StubAttachment("a1", "report.pdf", PDF, b"%PDF report v1"),
                StubAttachment("a2", "notes.txt", TEXT, b"notes v1"),
            ],
        )
    )
    stub.add(
        StubPage(
            id="102",
            space=SPACE,
            title="Diagram",
            html="<p>beta text</p>",
            attachments=[StubAttachment("a3", "scheme.png", PNG, b"PNG bytes")],
        )
    )
    stub.add(StubPage(id="103", space=SPACE, title="Empty", html="<p>gamma</p>"))


async def _stand(stub: ConfluenceStub, server: LiveServer, cfg: PostgresStoreConfig):
    stand = IngestStand(stub, server.port, cfg)
    await _wipe(cfg)
    return stand


async def _wipe(cfg: PostgresStoreConfig) -> None:
    pool = AsyncPostgresPool(cfg.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            for ident in (cfg.tables.chunks_ident(), cfg.tables.sources_ident()):
                statement = sql.SQL("delete from {} where collection = %s").format(
                    ident
                )
                async with conn.transaction():
                    await conn.execute(statement, (COLLECTION,))
    finally:
        await pool.close()


class TestFirstRun:
    async def test_pages_and_attachments_land_in_index_and_ledger(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            stats = await stand.run(IngestScope.space(SPACE))

        if stats["failed"] != 0:
            raise AssertionError(f"failed: {stats}")
        if stats["indexed"] <= 0:
            raise AssertionError(f"indexed: {stats}")

        page = await stand.record(stand.page_source("101"))
        if page is None:
            raise AssertionError("page 101 must be in the ledger")
        if page.fingerprint != "v1":
            raise AssertionError(f"page fingerprint: {page.fingerprint}")
        if not page.content_hash:
            raise AssertionError("page body hash must be recorded")

        att = await stand.record(stand.attachment_source("101", "report.pdf"))
        if att is None:
            raise AssertionError("attachment must be in the ledger")
        if att.parent != stand.page_source("101"):
            raise AssertionError(f"attachment parent: {att.parent}")
        if att.grade != int(ParseGrade.TEXT):
            raise AssertionError(f"attachment grade: {att.grade}")

        if await stand.chunk_count(stand.attachment_source("101", "notes.txt")) != 1:
            raise AssertionError("notes.txt must give one chunk")

    async def test_image_without_ocr_is_not_downloaded(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE), ocr=False)

        if stand.reader.parsed("scheme.png") != 0:
            raise AssertionError("image must not be parsed without ocr")
        if await stand.record(stand.attachment_source("102", "scheme.png")) is not None:
            raise AssertionError("skipped image must not be recorded")


class TestSecondRun:
    async def test_unchanged_sources_cost_no_body_requests(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stats = await stand.run(IngestScope.space(SPACE))

        if stats["indexed"] != 0:
            raise AssertionError(f"second run must index nothing: {stats}")
        if stats["skipped_unchanged"] != 5:
            raise AssertionError(f"3 pages + 2 attachments unchanged: {stats}")
        if stub.calls[StubRoute.BODY] != 0:
            raise AssertionError(f"page bodies requested: {stub.calls}")
        if stub.calls[StubRoute.DOWNLOAD] != 0:
            raise AssertionError(f"attachments downloaded: {stub.calls}")
        if stand.reader.reads:
            raise AssertionError(f"nothing must be parsed: {stand.reader.reads}")

    async def test_edited_page_is_reindexed_alone(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.pages["101"].edit(html="<h1>Overview</h1><p>alpha text changed</p>")
            stats = await stand.run(IngestScope.space(SPACE))

        if stub.calls[StubRoute.BODY] != 1:
            raise AssertionError(f"only the edited page body: {stub.calls}")
        if stub.calls[StubRoute.DOWNLOAD] != 0:
            raise AssertionError(f"attachments must stay: {stub.calls}")
        if stats["indexed"] <= 0:
            raise AssertionError(f"edited page must be indexed: {stats}")

        page = await stand.record(stand.page_source("101"))
        if page is None or page.fingerprint != "v2":
            raise AssertionError(f"page fingerprint after edit: {page}")

    async def test_renamed_page_is_reindexed(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.pages["103"].edit(title="Empty renamed")
            await stand.run(IngestScope.space(SPACE))

        if stand.reader.parsed("/rest/api/content/103") != 1:
            raise AssertionError("renamed page must be parsed again")

    async def test_reuploaded_same_bytes_are_downloaded_but_not_parsed(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            report = stub.pages["101"].attachment("report.pdf")
            report.upload(report.content, when="2026-02-01T00:00:00.000Z")
            stats = await stand.run(IngestScope.space(SPACE))

        if stub.calls[StubRoute.DOWNLOAD] != 1:
            raise AssertionError(f"one download expected: {stub.calls}")
        if stand.reader.parsed("report.pdf") != 0:
            raise AssertionError("same bytes must not be parsed")
        if stats["indexed"] != 0:
            raise AssertionError(f"nothing to upsert: {stats}")

        att = await stand.record(stand.attachment_source("101", "report.pdf"))
        if att is None or not att.fingerprint.startswith("v2:"):
            raise AssertionError(f"fingerprint must follow the new version: {att}")

    async def test_replaced_attachment_is_parsed(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            notes = stub.pages["101"].attachment("notes.txt")
            notes.upload(b"notes v2 rewritten", when="2026-02-01T00:00:00.000Z")
            stats = await stand.run(IngestScope.space(SPACE))

        if stand.reader.parsed("notes.txt") != 1:
            raise AssertionError("replaced attachment must be parsed")
        if stats["indexed"] != 1:
            raise AssertionError(f"one chunk upserted: {stats}")

    async def test_shrunk_page_drops_its_tail_chunks(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        long_html = "<p>" + " ".join(["word"] * 400) + "</p>"
        stub.add(StubPage(id="201", space=SPACE, title="Long", html=long_html))
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            before = await stand.chunk_count(stand.page_source("201"))
            stub.pages["201"].edit(html="<p>short</p>")
            stats = await stand.run(IngestScope.space(SPACE))
            after = await stand.chunk_count(stand.page_source("201"))

        if before <= 1:
            raise AssertionError(f"long page must give several chunks: {before}")
        if after != 1:
            raise AssertionError(f"short page must keep one chunk: {after}")
        if stats["deleted_chunks"] != before - 1:
            raise AssertionError(f"tail chunks deleted: {stats}")


class TestAdditions:
    async def test_new_page_is_indexed_alone(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.add(StubPage(id="104", space=SPACE, title="Fresh", html="<p>new</p>"))
            stats = await stand.run(IngestScope.space(SPACE))

        if stub.calls[StubRoute.BODY] != 1:
            raise AssertionError(f"only the new page body: {stub.calls}")
        if stats["indexed"] != 1:
            raise AssertionError(f"one new chunk: {stats}")
        if stats["skipped_unchanged"] != 5:
            raise AssertionError(f"old sources untouched: {stats}")
        if await stand.record(stand.page_source("104")) is None:
            raise AssertionError("new page must be in the ledger")

    async def test_new_attachment_is_downloaded_alone(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.pages["103"].attachments.append(
                StubAttachment("a7", "extra.txt", TEXT, b"extra text")
            )
            stats = await stand.run(IngestScope.space(SPACE))

        if stub.calls[StubRoute.DOWNLOAD] != 1:
            raise AssertionError(f"only the new attachment: {stub.calls}")
        if stub.calls[StubRoute.BODY] != 0:
            raise AssertionError(f"page bodies stay untouched: {stub.calls}")
        if stats["indexed"] != 1:
            raise AssertionError(f"one new chunk: {stats}")
        if await stand.chunk_count(stand.attachment_source("103", "extra.txt")) != 1:
            raise AssertionError("new attachment must be indexed")

        att = await stand.record(stand.attachment_source("103", "extra.txt"))
        if att is None or att.parent != stand.page_source("103"):
            raise AssertionError(f"new attachment belongs to its page: {att}")


class TestAttachmentsFlag:
    async def test_run_without_attachments_keeps_indexed_ones(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE), attachments=True)
            stats = await stand.run(IngestScope.space(SPACE), attachments=False)

        if stub.calls[StubRoute.DOWNLOAD] != 0:
            raise AssertionError(f"no downloads without attachments: {stub.calls}")
        if stats["deleted_sources"] != 0:
            raise AssertionError(f"attachments must survive: {stats}")
        if await stand.chunk_count(stand.attachment_source("101", "notes.txt")) != 1:
            raise AssertionError("attachment chunks must survive")

    async def test_removed_attachment_is_forgotten_even_without_attachments(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE), attachments=True)
            page = stub.pages["101"]
            page.attachments = [page.attachment("report.pdf")]
            stats = await stand.run(IngestScope.space(SPACE), attachments=False)

        if stats["deleted_sources"] != 1:
            raise AssertionError(f"removed attachment must go: {stats}")
        if await stand.chunk_count(stand.attachment_source("101", "notes.txt")) != 0:
            raise AssertionError("removed attachment chunks must go")
        if await stand.record(stand.attachment_source("101", "notes.txt")) is not None:
            raise AssertionError("removed attachment record must go")


class TestOcrGrade:
    async def test_ocr_run_reparses_text_grade_attachments_once(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE), ocr=False)
            await stand.run(IngestScope.space(SPACE), ocr=True)
            with_ocr = stand.reader.parsed("report.pdf") + stand.reader.parsed(
                "scheme.png"
            )
            await stand.run(IngestScope.space(SPACE), ocr=True)
            again = len(stand.reader.reads)
            await stand.run(IngestScope.space(SPACE), ocr=False)
            downgraded = len(stand.reader.reads)

        if with_ocr != 2:
            raise AssertionError(f"pdf and image must be parsed with ocr: {with_ocr}")
        if again != 0:
            raise AssertionError(f"second ocr run must parse nothing: {again}")
        if downgraded != 0:
            raise AssertionError(f"ocr grade must not be downgraded: {downgraded}")

        image = await stand.record(stand.attachment_source("102", "scheme.png"))
        if image is None or image.grade != int(ParseGrade.OCR):
            raise AssertionError(f"image grade: {image}")


class TestDeletedPages:
    async def test_deleted_page_goes_with_its_attachments(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.delete("101")
            stats = await stand.run(IngestScope.space(SPACE))

        if stats["deleted_sources"] != 3:
            raise AssertionError(f"page and two attachments must go: {stats}")
        if await stand.chunk_count(stand.page_source("101")) != 0:
            raise AssertionError("deleted page chunks must go")
        if await stand.chunk_count(stand.attachment_source("101", "report.pdf")) != 0:
            raise AssertionError("deleted page attachment chunks must go")
        if await stand.record(stand.page_source("101")) is not None:
            raise AssertionError("deleted page record must go")

    async def test_page_outside_the_query_survives(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stats = await stand.run(IngestScope.query('id = "103"'))

        if stats["deleted_sources"] != 0:
            raise AssertionError(f"existing pages must survive a narrow query: {stats}")
        if stub.calls[StubRoute.SEARCH] != 2:
            raise AssertionError(f"discovery plus one probe batch: {stub.calls}")

    async def test_single_page_scope_does_not_probe(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.delete("102")
            stats = await stand.run(IngestScope.page("103"))

        if stats["deleted_sources"] != 0:
            raise AssertionError(f"single page must not touch others: {stats}")
        if stub.calls[StubRoute.SEARCH] != 1:
            raise AssertionError(f"no probe for a single page: {stub.calls}")


class TestFailures:
    async def test_broken_page_is_counted_and_kept(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            page = stub.pages["101"]
            page.edit(html="<p>new</p>")
            page.broken = True
            stats = await stand.run(IngestScope.space(SPACE))

        if stats["failed"] != 1:
            raise AssertionError(f"one failed page: {stats}")
        if stats["deleted_sources"] != 0:
            raise AssertionError(f"failed page must not be treated as gone: {stats}")
        if await stand.chunk_count(stand.page_source("101")) == 0:
            raise AssertionError("old chunks of a failed page must survive")

    async def test_failed_attachment_does_not_lose_the_page(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.pages["101"].attachments.append(
            StubAttachment("a9", "broken.pdf", PDF, b"%PDF broken", broken=True)
        )
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            stats = await stand.run(IngestScope.space(SPACE))

        if stats["failed"] != 1:
            raise AssertionError(f"one failed attachment: {stats}")
        if await stand.chunk_count(stand.page_source("101")) == 0:
            raise AssertionError("page must be indexed")
        if await stand.record(stand.attachment_source("101", "broken.pdf")) is not None:
            raise AssertionError("failed attachment must not be recorded")


class TestManyAttachments:
    async def test_expansion_limit_is_followed_by_full_listing(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        attachments: list[StubAttachment] = []
        for n in range(30):
            attachments.append(
                StubAttachment(f"m{n}", f"file{n:02d}.txt", TEXT, f"text {n}".encode())
            )

        stub.add(
            StubPage(
                id="301",
                space=SPACE,
                title="Many",
                html="<p>many</p>",
                attachments=attachments,
            )
        )
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            stats = await stand.run(IngestScope.space(SPACE))

        if stub.calls[StubRoute.ATTACHMENTS] != 3:
            raise AssertionError(f"30 attachments by 10 per listing page: {stub.calls}")
        if stub.calls[StubRoute.DOWNLOAD] != 30:
            raise AssertionError(f"every attachment downloaded: {stub.calls}")
        if stats["failed"] != 0:
            raise AssertionError(f"failed: {stats}")
        if len(await stand.sources()) != 31:
            raise AssertionError("page and 30 attachments in the ledger")
