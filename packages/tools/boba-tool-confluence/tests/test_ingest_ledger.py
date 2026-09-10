"""Реестр источников на живом стенде: повтор не качает, удалённое уходит.

Настоящие: конвейер, транспорт Confluence, discovery, реестр и хранилище чанков
в postgres на схеме стенда (боевые миграции). Заглушкой служит только сам
Confluence (uvicorn), эмбеддер отдаёт нулевые векторы.

pytest -m integration.
"""

from __future__ import annotations

import asyncio
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
from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ConfluenceMarks,
    ConfluenceSourceId,
    ParseGrade,
    TableShape,
)
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
from boba.tool.confluence.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.indexing_log import (
    IngestProgress,
    LoggingChunker,
    LoggingChunkStore,
    LoggingSourceLedger,
)
from boba.tool.confluence.ingest_base import (
    ConfluenceIngest,
    ConfluenceIngestConfig,
    IngestReport,
    IngestScope,
)
from boba.tool.confluence.request_sources import ConfluenceRest
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
        return ConfluenceSourceId.of(self.connection().profile, str(path))

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
    ) -> IngestReport:
        self.stub.reset_calls()
        self.reader.reads.clear()
        # счёт ведётся на прогон, как в теле инструмента
        self.progress = IngestProgress(LOGGER)
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
        params = ChunkerParams(
            chunk_size=200,
            chunk_overlap=0,
            table_shape=TableShape(
                row_layout_max_columns=4,
                row_layout_min_rows=3,
            ),
        )
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
        """Все записи реестра коллекции: пустая метка прогона ничего не видела."""
        ids: list[str] = []
        async for record in self.ledger.unseen_roots(_scope_of(SPACE), ""):
            ids.append(str(record.source_id))
            async for child in self.ledger.children(record.source_id):
                ids.append(str(child.source_id))

        return ids


def _scope_of(space: str) -> str:
    return IngestScope.space(space).owned()


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

        if stats.pages.failed + stats.attachments.failed != 0:
            raise AssertionError(f"failed: {stats}")
        if stats.pages.chunks + stats.attachments.chunks <= 0:
            raise AssertionError(f"indexed: {stats}")

        page = await stand.record(stand.page_source("101"))
        if page is None:
            raise AssertionError("page 101 must be in the ledger")
        if page.fingerprint != ConfluenceMarks.page(1).fingerprint:
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


class TestReport:
    """Отчёт читается без догадок: ноль записанных и ноль найденных различимы."""

    async def test_found_is_visible_when_nothing_changed(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            report = await stand.run(IngestScope.space(SPACE))

        if report.pages.found != 3:
            raise AssertionError(f"pages are still found: {report.pages}")
        if report.pages.unchanged != 3:
            raise AssertionError(f"pages are unchanged, not missing: {report.pages}")
        if report.pages.chunks != 0:
            raise AssertionError(f"nothing rewritten: {report.pages}")

    async def test_empty_query_shows_zero_found(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.spaces.add("EMPTY")
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            report = await stand.run(IngestScope.space("EMPTY"))

        if report.pages.found != 0:
            raise AssertionError(f"an empty space finds nothing: {report.pages}")
        if report.pages.unchanged != 0:
            raise AssertionError(f"nothing to skip either: {report.pages}")

    async def test_skipped_attachments_carry_their_reason(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            report = await stand.run(IngestScope.space(SPACE), attachments=False)

        if report.attachments.found != 3:
            raise AssertionError(f"attachments are counted: {report.attachments}")
        if report.attachments.skipped != 3:
            raise AssertionError(f"all of them skipped: {report.attachments}")
        if "not requested" not in report.attachments.skipped_reasons:
            raise AssertionError(f"the reason is named: {report.attachments}")

    async def test_failure_reason_reaches_the_report(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.pages["101"].broken = True
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            report = await stand.run(IngestScope.space(SPACE))

        if report.pages.failed != 1:
            raise AssertionError(f"one page failed: {report.pages}")
        if "500" not in report.pages.error:
            raise AssertionError(f"the status is in the report: {report.pages.error}")


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

        if stats.pages.chunks + stats.attachments.chunks != 0:
            raise AssertionError(f"second run must index nothing: {stats}")
        if stats.pages.unchanged + stats.attachments.unchanged != 5:
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
        if stats.pages.chunks + stats.attachments.chunks <= 0:
            raise AssertionError(f"edited page must be indexed: {stats}")

        page = await stand.record(stand.page_source("101"))
        if page is None or page.fingerprint != ConfluenceMarks.page(2).fingerprint:
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
        if stats.pages.chunks + stats.attachments.chunks != 0:
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
        if stats.pages.chunks + stats.attachments.chunks != 1:
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
        if stats.pages.chunks_deleted != before - 1:
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
        if stats.pages.chunks + stats.attachments.chunks != 1:
            raise AssertionError(f"one new chunk: {stats}")
        if stats.pages.unchanged + stats.attachments.unchanged != 5:
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
        if stats.pages.chunks + stats.attachments.chunks != 1:
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
        if stats.pages.deleted + stats.attachments.deleted != 0:
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

        if stats.pages.deleted + stats.attachments.deleted != 1:
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

        if stats.pages.deleted + stats.attachments.deleted != 3:
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

        if stats.pages.deleted + stats.attachments.deleted != 0:
            raise AssertionError(f"existing pages must survive a narrow query: {stats}")
        if stub.calls[StubRoute.SEARCH] != 1:
            raise AssertionError(
                f"a query owns nothing, so it never probes: {stub.calls}"
            )

    async def test_single_page_scope_reads_only_its_page(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.delete("102")
            stats = await stand.run(IngestScope.page("103"))

        if stats.pages.deleted + stats.attachments.deleted != 0:
            raise AssertionError(f"single page must not touch others: {stats}")
        if stub.calls[StubRoute.SEARCH] != 0:
            raise AssertionError(
                f"a single page goes by id, not by search: {stub.calls}"
            )
        if stub.calls[StubRoute.SPACE_CONTENT] != 0:
            raise AssertionError(f"a single page does not walk its space: {stub.calls}")


class TestParallelRuns:
    """Соседние спейсы обходятся одновременно и не трогают чужое."""

    async def test_runs_over_two_spaces_keep_each_other_sources(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.add(
            StubPage(
                id="401",
                space="OTHER",
                title="Neighbour",
                html="<p>neighbour</p>",
                attachments=[StubAttachment("b1", "near.txt", TEXT, b"near text")],
            )
        )
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            await stand.run(IngestScope.space("OTHER"))

            first, second = await asyncio.gather(
                stand.run(IngestScope.space(SPACE)),
                stand.run(IngestScope.space("OTHER")),
            )

        deleted = (
            first.pages.deleted
            + first.attachments.deleted
            + second.pages.deleted
            + second.attachments.deleted
        )
        if deleted != 0:
            raise AssertionError(
                f"parallel runs must delete nothing: {first}, {second}"
            )
        if await stand.chunk_count(stand.attachment_source("101", "notes.txt")) != 1:
            raise AssertionError("a neighbour run must not drop these chunks")
        if await stand.record(stand.attachment_source("401", "near.txt")) is None:
            raise AssertionError("the neighbour attachment must survive")


class TestGoneAnswer:
    """404 от Confluence — прямой ответ «нет такого», а не догадка."""

    async def test_single_page_run_deletes_a_page_that_answers_404(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.pages["101"].version += 1
            stub.pages["101"].missing = True
            report = await stand.run(IngestScope.page("101"))

        if report.pages.deleted != 1:
            raise AssertionError(f"the page is gone: {report.pages}")
        if report.attachments.deleted != 2:
            raise AssertionError(f"its attachments go with it: {report.attachments}")
        if await stand.chunk_count(stand.page_source("101")) != 0:
            raise AssertionError("chunks of a gone page must go")
        if await stand.record(stand.attachment_source("101", "notes.txt")) is not None:
            raise AssertionError("its attachment record must go")

    async def test_failed_page_is_not_deleted(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.pages["101"].version += 1
            stub.pages["101"].broken = True
            report = await stand.run(IngestScope.space(SPACE))

        if report.pages.failed != 1:
            raise AssertionError(f"the page failed: {report.pages}")
        if report.pages.deleted != 0:
            raise AssertionError(f"a failed page stays in the index: {report.pages}")
        if await stand.chunk_count(stand.page_source("101")) == 0:
            raise AssertionError("its chunks stay too")


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

        if stats.pages.failed + stats.attachments.failed != 1:
            raise AssertionError(f"one failed page: {stats}")
        if stats.pages.deleted + stats.attachments.deleted != 0:
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

        if stats.pages.failed + stats.attachments.failed != 1:
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
        if stats.pages.failed + stats.attachments.failed != 0:
            raise AssertionError(f"failed: {stats}")
        if len(await stand.sources()) != 31:
            raise AssertionError("page and 30 attachments in the ledger")


class TestArchivedSpace:
    """Архивный спейс: поиск его контент не отдаёт, список спейса — отдаёт.

    Confluence держит архивные спейсы вне поискового индекса, поэтому обход
    по CQL находит там ноль страниц, хотя страницы живы и читаются.
    """

    async def test_archived_space_pages_are_indexed(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.archive(SPACE)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            stats = await stand.run(IngestScope.space(SPACE), attachments=False)

        if stats.pages.found != 3:
            raise AssertionError(f"an archived space still lists its pages: {stats}")
        if stats.pages.indexed != 3:
            raise AssertionError(f"archived pages must be indexed: {stats}")
        if await stand.chunk_count(stand.page_source("101")) == 0:
            raise AssertionError("archived page chunks must be stored")

    async def test_archived_space_survives_the_next_run(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        """Спейс архивировали после индексации: cleanup не считает его пустым."""
        stub = ConfluenceStub()
        _space(stub)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            await stand.run(IngestScope.space(SPACE))
            stub.archive(SPACE)
            stats = await stand.run(IngestScope.space(SPACE))

        if stats.pages.deleted + stats.attachments.deleted != 0:
            raise AssertionError(f"archiving deletes nothing from the index: {stats}")
        if await stand.chunk_count(stand.page_source("101")) == 0:
            raise AssertionError("archived page chunks must survive")

    async def test_archived_page_is_indexed_by_id(
        self, store_cfg: PostgresStoreConfig
    ) -> None:
        stub = ConfluenceStub()
        _space(stub)
        stub.archive(SPACE)
        async with LiveServer(stub.app()) as server:
            stand = await _stand(stub, server, store_cfg)
            stats = await stand.run(IngestScope.page("103"), attachments=False)

        if stats.pages.indexed != 1:
            raise AssertionError(f"a page of an archived space is readable: {stats}")
        if await stand.chunk_count(stand.page_source("103")) == 0:
            raise AssertionError("archived page chunks must be stored")
