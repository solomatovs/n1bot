"""Сорвавшаяся страница не роняет прогон ingest: skip_failed на живом сервере.

Стенд — настоящий Confluence-endpoint на uvicorn: одна страница отдаётся, другая
отвечает 500, у отдаваемой одно вложение целое, другое тоже 500. Прогон идёт
через настоящие HttpTransport, ConfluenceContentTransport и Pipeline; в памяти
живут только хранилище чанков и эмбеддер.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest
import uvicorn
from fastapi import FastAPI, Response
from ingest_stand import MemoryChunkStore, TextReader, ZeroEmbedder

from boba.indexing import (
    CollectionId,
    CollectionScopedView,
    IndexerConfig,
    IndexStats,
    NoneCleanup,
    Pipeline,
    TransportError,
)
from boba.tool.kb.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter, AttachmentGate
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
from boba.transport.http import HttpProfile

pytestmark = pytest.mark.anyio

LOGGER = logging.getLogger("test.ingest.skip_failed")
COLLECTION = CollectionId("kb_test")

GOOD_PAGE = "101"
BROKEN_PAGE = "102"
GOOD_ATTACHMENT = "report.txt"
BROKEN_ATTACHMENT = "broken.txt"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class ConfluenceStub:
    """Confluence-endpoint, у которого часть ответов — 500."""

    @staticmethod
    def app() -> FastAPI:
        app = FastAPI()

        @app.get("/rest/api/content/{page_id}")
        async def page(page_id: str) -> Response:
            if page_id == BROKEN_PAGE:
                return Response(status_code=500)

            body = ConfluenceStub.page_json(page_id)
            return Response(content=body, media_type="application/json")

        @app.get("/download/attachments/{page_id}/{name}")
        async def attachment(page_id: str, name: str) -> Response:
            if name == BROKEN_ATTACHMENT:
                return Response(status_code=500)

            return Response(content=b"attachment payload", media_type="text/plain")

        return app

    @staticmethod
    def page_json(page_id: str) -> str:
        results: list[dict[str, Any]] = []
        for name in (GOOD_ATTACHMENT, BROKEN_ATTACHMENT):
            results.append(
                {
                    "id": f"att-{name}",
                    "title": name,
                    "extensions": {"mediaType": "text/plain", "fileSize": 18},
                    "_links": {"download": f"/download/attachments/{page_id}/{name}"},
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
            "_links": {"webui": f"/pages/{page_id}"},
        }
        return json.dumps(page)


class LiveServer:
    """uvicorn на свободном порту в текущем цикле событий."""

    STARTUP_POLL_SEC: ClassVar[float] = 0.02

    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._server.serve())

        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("uvicorn stopped before it started")

            await asyncio.sleep(self.STARTUP_POLL_SEC)

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    @property
    def base_url(self) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"


class SkipStand:
    """Прогон ingest против стенда: наружу — итог, прогресс и хранилище."""

    def __init__(self, base_url: str, *, skip_failed: bool) -> None:
        self.store = MemoryChunkStore()
        self.progress = IngestProgress(LOGGER)
        self._base_url = base_url
        self._skip_failed = skip_failed

    def connection(self) -> ConfluenceConnection:
        profile = HttpProfile(
            base_url=self._base_url,
            retry_attempts=1,
            retry_backoff_sec=0.0,
            timeout_sec=10.0,
        )
        return ConfluenceConnection(profile=profile, body_format="view")

    async def run(self) -> IndexStats:
        conn = self.connection()
        source = ConfluencePagesRequestSource(
            base_url=conn.base_url,
            page_ids=(GOOD_PAGE, BROKEN_PAGE),
            body_format=conn.body_format,
            progress=self.progress,
        )
        transport = ConfluenceContentTransport.from_connection(
            conn,
            progress=self.progress,
            gate=AttachmentGate.of(AttachmentFilter(), "*", ocr_enabled=True),
            skip_failed=self._skip_failed,
        )
        view: CollectionScopedView[str] = CollectionScopedView(
            store=LoggingChunkStore(self.store, LOGGER),
            embedder=ZeroEmbedder(),
            collection=COLLECTION,
        )
        pipeline: Pipeline[ConfluenceRequest, str] = Pipeline(
            source=source,
            transport=transport,
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
            config=IndexerConfig(
                workers=1,
                cleanup=NoneCleanup(),
                skip_failed=self._skip_failed,
            ),
        )
        try:
            return await LoggedIndexRun.drain(events, LOGGER, self.progress)
        finally:
            await transport.close()


class TestSkipFailed:
    """skip_failed=true: 500 стоит одной страницы, а не всего прогона."""

    async def test_broken_page_is_skipped_and_the_rest_is_indexed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with LiveServer(ConfluenceStub.app()) as server:
            stand = SkipStand(server.base_url, skip_failed=True)
            with caplog.at_level(logging.INFO):
                stats = await stand.run()

        if stats.sources_failed != 1:
            raise AssertionError("stats.sources_failed == 1")
        if stats.chunks_upserted <= 0:
            raise AssertionError("stats.chunks_upserted > 0")
        if not stand.store.chunks:
            raise AssertionError("stand.store.chunks")

    async def test_broken_attachment_does_not_lose_the_page(self) -> None:
        async with LiveServer(ConfluenceStub.app()) as server:
            stand = SkipStand(server.base_url, skip_failed=True)
            stats = await stand.run()

        sources: set[str] = set()
        for chunk in stand.store.chunks.values():
            sources.add(str(chunk.source_id))

        page_indexed = False
        attachment_indexed = False
        for source in sources:
            if source.endswith(f"/rest/api/content/{GOOD_PAGE}"):
                page_indexed = True
            if source.endswith(GOOD_ATTACHMENT):
                attachment_indexed = True

        if not page_indexed:
            raise AssertionError("page_indexed")
        if not attachment_indexed:
            raise AssertionError("attachment_indexed")
        if stats.chunks_upserted <= 0:
            raise AssertionError("stats.chunks_upserted > 0")

    async def test_failed_counter_shows_up_in_progress(self) -> None:
        async with LiveServer(ConfluenceStub.app()) as server:
            stand = SkipStand(server.base_url, skip_failed=True)
            await stand.run()

        summary = stand.progress.render()
        if "failed 0" in summary:
            raise AssertionError('"failed 0" not in summary')


class TestStrictRun:
    """skip_failed=false: первая же сорвавшаяся страница обрывает прогон."""

    async def test_run_stops_on_the_first_failure(self) -> None:
        async with LiveServer(ConfluenceStub.app()) as server:
            stand = SkipStand(server.base_url, skip_failed=False)
            with pytest.raises(TransportError):
                await stand.run()
