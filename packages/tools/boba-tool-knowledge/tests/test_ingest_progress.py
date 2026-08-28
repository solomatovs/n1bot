"""Наблюдаемость прогона ingest: строка прогресса и логи вокруг каждого IO.

Прогон собран из настоящих Pipeline, CollectionScopedView и обёрток
наблюдения; заменены только внешние границы — хранилище держит чанки в памяти,
эмбеддер отдаёт нули, транспорт возвращает заготовленные ответы Confluence.
Проверяется то, ради чего логи и заводились: по журналу видно, на какой
операции прогон стоит и сколько ещё осталось.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ingest_stand import MemoryChunkStore, TextReader, ZeroEmbedder

from boba.indexing import (
    ChunkStream,
    CollectionId,
    CollectionScopedView,
    IndexerConfig,
    NoneCleanup,
    Pipeline,
    RawDocument,
    RequestSource,
    SourceId,
    Transport,
)
from boba.tool.kb.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.kb.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    ConfluenceKeys,
)
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.request_sources import (
    ConfluencePagesRequestSource,
    ConfluenceRequest,
)
from boba.tool.kb.indexing_log import (
    IngestProgress,
    LoggedIndexRun,
    LoggingChunker,
    LoggingChunkStore,
    LoggingReader,
)

pytestmark = pytest.mark.anyio

LOGGER = logging.getLogger("test.ingest.progress")
COLLECTION = CollectionId("kb_test")
BASE_URL = "https://confluence.example.local"
PAGE_IDS = ("101", "102")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Прогон не зависит от сессии chainlit."""


class _CannedTransport(Transport[ConfluenceRequest]):
    """Заготовленные ответы Confluence: страницы с JSON, вложения — байтами."""

    def __init__(self, attachments_per_page: int) -> None:
        self._attachments_per_page = attachments_per_page

    async def close(self) -> None:
        return None

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        page_id = request.metadata.get(ConfluenceKeys.PAGE_ID) or "?"
        info = request.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
        if info is None:
            return SourceId(f"{BASE_URL}/rest/api/content/{page_id}")

        return SourceId(f"{BASE_URL}{info.download_path}")

    async def fetch(self, request: ConfluenceRequest) -> AsyncIterator[RawDocument]:
        info = request.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
        if info is not None:
            yield RawDocument(
                handle=ChunkStream.of(b"attachment payload"),
                source_id=self.source_id(request),
                metadata=request.metadata,
            )
            return

        page_id = request.metadata.get(ConfluenceKeys.PAGE_ID) or "?"
        yield RawDocument(
            handle=ChunkStream.of(self._page_json(page_id).encode("utf-8")),
            source_id=self.source_id(request),
            metadata=request.metadata,
        )

    def _page_json(self, page_id: str) -> str:
        results: list[dict[str, Any]] = []
        for index in range(self._attachments_per_page):
            results.append(
                {
                    "id": f"att{page_id}{index}",
                    "title": f"report-{index}.txt",
                    "extensions": {"mediaType": "text/plain", "fileSize": 18},
                    "_links": {
                        "download": f"/download/attachments/{page_id}/{index}.txt",
                        "webui": f"/pages/viewpage.action?pageId={page_id}",
                    },
                    "version": {"number": 1},
                }
            )

        page = {
            "id": page_id,
            "title": f"Page {page_id}",
            "space": {"key": "DOCS"},
            "version": {"number": 1, "when": "2026-01-01T00:00:00.000Z"},
            "body": {"view": {"value": f"<h1>Page {page_id}</h1><p>text</p>"}},
            "children": {"attachment": {"results": results}},
            "_links": {"base": BASE_URL, "webui": f"/pages/{page_id}"},
        }
        return json.dumps(page)


class IngestStand:
    """Прогон ingest на настоящем Pipeline; наружу — журнал и прогресс."""

    def __init__(self, *, attachments_per_page: int) -> None:
        self.store = MemoryChunkStore()
        self.progress = IngestProgress(LOGGER)
        self._attachments_per_page = attachments_per_page

    def source(self) -> RequestSource[ConfluenceRequest]:
        return ConfluencePagesRequestSource(
            base_url=BASE_URL,
            page_ids=PAGE_IDS,
            body_format="view",
            progress=self.progress,
        )

    def transport(self) -> ConfluenceContentTransport:
        return ConfluenceContentTransport(
            inner=_CannedTransport(self._attachments_per_page),
            body_format="view",
            base_url=BASE_URL,
            progress=self.progress,
            gate=AttachmentGate.of(AttachmentFilter(), "*", ocr_enabled=True),
            skip_failed=True,
        )

    async def run(self) -> None:
        view: CollectionScopedView[str] = CollectionScopedView(
            store=LoggingChunkStore(self.store, LOGGER),
            embedder=ZeroEmbedder(),
            collection=COLLECTION,
        )
        pipeline: Pipeline[ConfluenceRequest, str] = Pipeline(
            source=self.source(),
            transport=self.transport(),
            reader=LoggingReader(TextReader(), LOGGER),
        )
        params = ChunkerParams(chunk_size=200, chunk_overlap=0)
        chunker = LoggingChunker(
            StructuralChunkerFactory.build(params), LOGGER, self.progress
        )
        events = pipeline.index(
            chunker=chunker,
            sink=view,
            query=view,
            config=IndexerConfig(workers=1, cleanup=NoneCleanup()),
        )
        await LoggedIndexRun.drain(events, LOGGER, self.progress)


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
        stand = IngestStand(attachments_per_page=1)
        with caplog.at_level(logging.INFO):
            await stand.run()

        if not (_matching(caplog, "db diff_by_hash start")):
            raise AssertionError('_matching(caplog, "db diff_by_hash start")')
        if not (_matching(caplog, "db diff_by_hash done")):
            raise AssertionError('_matching(caplog, "db diff_by_hash done")')
        if not (_matching(caplog, "db upsert start")):
            raise AssertionError('_matching(caplog, "db upsert start")')
        if not (_matching(caplog, "db upsert done")):
            raise AssertionError('_matching(caplog, "db upsert done")')

    async def test_page_and_attachment_fetch_are_bracketed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stand = IngestStand(attachments_per_page=2)
        with caplog.at_level(logging.INFO):
            await stand.run()

        if len(_matching(caplog, "fetch page start")) != len(PAGE_IDS):
            raise AssertionError('len(_matching(caplog, "fetch page start")) == len(P…')
        if len(_matching(caplog, "fetch page done")) != len(PAGE_IDS):
            raise AssertionError('len(_matching(caplog, "fetch page done")) == len(PA…')
        if len(_matching(caplog, "fetch attachment start")) != 2 * len(PAGE_IDS):
            raise AssertionError('len(_matching(caplog, "fetch attachment start")) ==…')
        if len(_matching(caplog, "fetch attachment done")) != 2 * len(PAGE_IDS):
            raise AssertionError('len(_matching(caplog, "fetch attachment done")) == …')

    async def test_read_and_chunking_are_bracketed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stand = IngestStand(attachments_per_page=0)
        with caplog.at_level(logging.INFO):
            await stand.run()

        if not (_matching(caplog, "read start")):
            raise AssertionError('_matching(caplog, "read start")')
        if not (_matching(caplog, "read done")):
            raise AssertionError('_matching(caplog, "read done")')
        if not (_matching(caplog, "chunking start")):
            raise AssertionError('_matching(caplog, "chunking start")')
        if not (_matching(caplog, "chunking done")):
            raise AssertionError('_matching(caplog, "chunking done")')


class TestProgress:
    """Строка прогресса отвечает на «сколько сделано и сколько осталось»."""

    async def test_counts_pages_attachments_and_chunks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stand = IngestStand(attachments_per_page=3)
        with caplog.at_level(logging.INFO):
            await stand.run()

        summary = stand.progress.render()
        if "pages 2/2" not in summary:
            raise AssertionError('"pages 2/2" in summary')
        if "attachments 6/6" not in summary:
            raise AssertionError('"attachments 6/6" in summary')
        if "chunks 0" in summary:
            raise AssertionError('"chunks 0" not in summary')
        if "failed 0" not in summary:
            raise AssertionError('"failed 0" in summary')

    async def test_open_discovery_is_marked(self) -> None:
        progress = IngestProgress(LOGGER)
        progress.pages_found(10)
        progress.page_done()

        if "pages 1/10+" not in progress.render():
            raise AssertionError('"pages 1/10+" in progress.render()')

        progress.pages_closed()

        if "pages 1/10" not in progress.render():
            raise AssertionError('"pages 1/10" in progress.render()')

    async def test_summary_is_logged_after_every_page(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stand = IngestStand(attachments_per_page=0)
        with caplog.at_level(logging.INFO):
            await stand.run()

        if len(_matching(caplog, "progress: spaces")) < len(PAGE_IDS):
            raise AssertionError('len(_matching(caplog, "progress: spaces")) >= len(P…')

    async def test_spaces_are_counted(self) -> None:
        progress = IngestProgress(LOGGER)
        progress.spaces_found(3)
        progress.space_done("DOCS")

        if "spaces 1/3" not in progress.render():
            raise AssertionError('"spaces 1/3" in progress.render()')
