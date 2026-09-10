"""Наблюдаемость прогона ingest: строка прогресса и логи вокруг каждого IO.

Прогон собран из настоящих Pipeline, discovery, транспорта Confluence и
обёрток наблюдения против живой заглушки Confluence на uvicorn; в памяти
живут только хранилище чанков, реестр источников и эмбеддер. Проверяется то,
ради чего логи и заводились: по журналу видно, на какой операции прогон
стоит и сколько ещё осталось.
"""

from __future__ import annotations

import logging

import pytest
from confluence_stand import ConfluenceStub, LiveServer, StubAttachment, StubPage
from ingest_stand import MemoryChunkStore, MemorySourceLedger, TextReader, ZeroEmbedder

from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ParseGrade,
    TableShape,
)
from boba.indexing import (
    CollectionId,
    CollectionScopedView,
    DispatchReader,
    IndexerConfig,
    NoProbe,
    Pipeline,
    ReaderId,
    TransportKeys,
)
from boba.tool.confluence.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.indexing_log import (
    IngestProgress,
    LoggedIndexRun,
    LoggingChunker,
    LoggingChunkStore,
    LoggingReader,
    LoggingSourceLedger,
)
from boba.tool.confluence.pipeline import ConfluenceSourceTransport
from boba.tool.confluence.request_sources import (
    ConfluenceDiscovery,
    ConfluenceRequest,
    SpaceListing,
)
from boba.transport.http.profile import HttpConnection, UrlScheme

pytestmark = pytest.mark.anyio

LOGGER = logging.getLogger("test.ingest.progress")
COLLECTION = CollectionId("kb_test")
SPACE = "DOCS"
PAGE_IDS = ("101", "102")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Прогон не зависит от сессии chainlit."""


def _stub(attachments_per_page: int) -> ConfluenceStub:
    stub = ConfluenceStub()
    for page_id in PAGE_IDS:
        attachments: list[StubAttachment] = []
        for index in range(attachments_per_page):
            attachments.append(
                StubAttachment(
                    f"att{page_id}{index}",
                    f"report-{index}.txt",
                    "text/plain",
                    b"attachment payload",
                )
            )

        stub.add(
            StubPage(
                id=page_id,
                space=SPACE,
                title=f"Page {page_id}",
                html=f"<h1>Page {page_id}</h1><p>text</p>",
                attachments=attachments,
            )
        )

    return stub


class IngestStand:
    """Прогон ingest на настоящем Pipeline; наружу — журнал и прогресс."""

    def __init__(self, port: int) -> None:
        self.store = MemoryChunkStore()
        self.ledger = MemorySourceLedger()
        self.progress = IngestProgress(LOGGER)
        profile = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=port,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            timeout_sec=10.0,
        )
        self.conn = ConfluenceConnection(profile=profile, body_format="view")

    async def run(self) -> None:
        view: CollectionScopedView[str] = CollectionScopedView(
            store=LoggingChunkStore(self.store, LOGGER),
            embedder=ZeroEmbedder(),
            collection=COLLECTION,
        )
        ledger = LoggingSourceLedger(self.ledger, LOGGER)
        source = ConfluenceDiscovery(
            conn=self.conn,
            listing=SpaceListing(SPACE),
            gate=AttachmentGate(allowed=AttachmentFilter(), requested=True, ocr=True),
            grade=ParseGrade.OCR,
            progress=self.progress,
        )
        reader: DispatchReader[str] = DispatchReader(
            by=TransportKeys.CONTENT_TYPE,
            routes={
                "text/html": LoggingReader(TextReader(), LOGGER),
                "text/plain": LoggingReader(TextReader(), LOGGER),
            },
            reader_id=ReaderId("test.dispatch"),
        )
        transport = ConfluenceSourceTransport.from_connection(self.conn)
        pipeline: Pipeline[ConfluenceRequest, str] = Pipeline(
            source=source,
            transport=transport,
            reader=reader,
            ledger=ledger,
            probe=NoProbe(),
        )
        params = ChunkerParams(
            chunk_size=200,
            chunk_overlap=0,
            table_shape=TableShape(
                row_layout_max_columns=4,
                row_layout_min_rows=3,
            ),
        )
        chunker = LoggingChunker(
            StructuralChunkerFactory.build(params), LOGGER, self.progress
        )
        events = pipeline.index(
            chunker=chunker,
            sink=view,
            config=IndexerConfig(workers=1, stamp="test"),
        )
        try:
            await LoggedIndexRun.drain(events, LOGGER, self.progress)
        finally:
            await transport.close()


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    messages: list[str] = []
    for record in caplog.records:
        messages.append(record.getMessage())

    return messages


def _matching(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    found: list[str] = []
    for message in _lines(caplog):
        if needle not in message:
            continue

        found.append(message)

    return found


class TestIoLogging:
    """У каждой операции ввода-вывода есть строка до и строка после."""

    async def test_every_db_operation_is_bracketed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(_stub(1).app()) as server:
            with caplog.at_level(logging.INFO):
                await IngestStand(server.port).run()

        for needle in (
            "db diff_by_hash start",
            "db diff_by_hash done",
            "db upsert start",
            "db upsert done",
            "ledger lookup start",
            "ledger record done",
        ):
            if not _matching(caplog, needle):
                raise AssertionError(f"no log line {needle!r}")

    async def test_page_and_attachment_fetch_are_bracketed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(_stub(2).app()) as server:
            with caplog.at_level(logging.INFO):
                await IngestStand(server.port).run()

        if len(_matching(caplog, "fetch page start")) != len(PAGE_IDS):
            raise AssertionError("one fetch page start per page")
        if len(_matching(caplog, "fetch page done")) != len(PAGE_IDS):
            raise AssertionError("one fetch page done per page")
        if len(_matching(caplog, "fetch attachment start")) != 2 * len(PAGE_IDS):
            raise AssertionError("one fetch attachment start per attachment")
        if len(_matching(caplog, "fetch attachment done")) != 2 * len(PAGE_IDS):
            raise AssertionError("one fetch attachment done per attachment")

    async def test_read_and_chunking_are_bracketed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(_stub(0).app()) as server:
            with caplog.at_level(logging.INFO):
                await IngestStand(server.port).run()

        for needle in ("read start", "read done", "chunking start", "chunking done"):
            if not _matching(caplog, needle):
                raise AssertionError(f"no log line {needle!r}")


class TestProgress:
    """Строка прогресса отвечает на «сколько сделано и сколько осталось»."""

    async def test_counts_pages_attachments_and_chunks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(_stub(3).app()) as server:
            stand = IngestStand(server.port)
            with caplog.at_level(logging.INFO):
                await stand.run()

        summary = stand.progress.render()
        if "pages 2/2" not in summary:
            raise AssertionError(f'"pages 2/2" in {summary}')
        if "attachments 6/6" not in summary:
            raise AssertionError(f'"attachments 6/6" in {summary}')
        if "chunks 0" in summary:
            raise AssertionError(f'"chunks 0" not in {summary}')
        if "failed 0" not in summary:
            raise AssertionError(f'"failed 0" in {summary}')

    async def test_open_discovery_is_marked(self) -> None:
        progress = IngestProgress(LOGGER)
        progress.pages_found(10)
        progress.page_done()

        if "pages 1/10+" not in progress.render():
            raise AssertionError('"pages 1/10+" in progress.render()')

        progress.pages_closed()

        if "pages 1/10" not in progress.render():
            raise AssertionError('"pages 1/10" in progress.render()')

    async def test_summary_is_logged_after_every_source(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(_stub(0).app()) as server:
            with caplog.at_level(logging.INFO):
                await IngestStand(server.port).run()

        if len(_matching(caplog, "progress: spaces")) < len(PAGE_IDS):
            raise AssertionError("progress line after every source")

    async def test_spaces_are_counted(self) -> None:
        progress = IngestProgress(LOGGER)
        progress.spaces_found(3)
        progress.space_done("DOCS")

        if "spaces 1/3" not in progress.render():
            raise AssertionError('"spaces 1/3" in progress.render()')
