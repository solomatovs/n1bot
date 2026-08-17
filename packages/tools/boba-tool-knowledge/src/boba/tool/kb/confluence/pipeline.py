"""Общий Confluence-pipeline стейдж: HTTP + JSON-decode + attachment fan-out.

ConfluenceContentTransport — единый Transport[HttpRequest], через который
ходят и download, и ingest. Внутри:

1. Если HttpRequest.metadata содержит ConfluenceKeys.ATTACHMENT_INFO —
   это уже attachment-request (его сгенерировал сам transport на предыдущей
   итерации page-request'а). Прозрачно делегируется во внутренний
   HttpTransport, без декодирования.
2. Иначе — page-request: внутренний transport отдаёт JSON ->
   ConfluenceJsonDecoder извлекает HTML и обогащает metadata (включая
   ConfluenceKeys.ATTACHMENTS и обновлённый TransportKeys.CONTENT_TYPE
   = text/html) -> yield декодированной страницы -> для каждого вложения
   из ATTACHMENTS строится attachment-HttpRequest через
   ConfluenceRest.make_attachment_request и тоже стримится через transport.

Yield-порядок per page-request: сам page (HTML), потом вложения в порядке
из Confluence JSON. Никаких list'ов между стадиями: stream-pipeline через
yield/yield from.

Download потребляет результат через ConfluenceContentTransport.iter_documents,
ingest подключает ConfluenceContentTransport напрямую в Pipeline
(декодинг уже выполнен внутри transport'а; reader должен быть
DispatchReader, потому что поток смешанный: HTML + произвольные
attachment-media-types).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace

from boba.indexing import (
    Metadata,
    RawDocument,
    RequestSource,
    SourceId,
    Transport,
    TransportKeys,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentGate,
    AttachmentVerdict,
    ConfluenceKeys,
    HttpKeys,
)
from boba.tool.kb.confluence.parsing import ConfluenceJsonDecoder
from boba.tool.kb.confluence.request_sources import (
    ConfluenceRequest,
    ConfluenceRest,
)
from boba.tool.kb.indexing_log import Elapsed, IngestProgress, LoggingStream
from boba.transport.http import (
    CancellableHttpTransport,
    HttpResponse,
    HttpTransport,
)

logger = logging.getLogger(__name__)

__all__ = ["ConfluenceContentTransport", "ConfluenceHttpTransport"]


class ConfluenceHttpTransport(Transport[ConfluenceRequest]):
    """ConfluenceRequest -> RawDocument: чистый HTTP + обогащение metadata.

    Оборачивает чистый HttpTransport: исполняет request.http, собирает
    RawDocument (source_id выводит сам из реально запрашиваемого URL —
    resolve_url без query; metadata + ключи из заголовков ответа).
    Lifecycle handle — у HttpTransport.fetch: handle живёт пока идёт
    итерация результата, закроется на выходе из этого generator'а.
    """

    def __init__(self, http: HttpTransport) -> None:
        self._http = http

    async def close(self) -> None:
        await self._http.close()

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        resolved = self._http.resolve_url(request.http)
        return SourceId(resolved.split("?", 1)[0])

    async def fetch(self, request: ConfluenceRequest) -> AsyncIterator[RawDocument]:
        async with self._http.fetch(request.http) as resp:
            yield RawDocument(
                handle=resp.stream,
                source_id=self.source_id(request),
                metadata=self._enrich(request.metadata, resp),
            )

    @staticmethod
    def _enrich(base: Metadata, resp: HttpResponse) -> Metadata:
        md = base
        h = resp.headers
        if etag := h.get("etag"):
            md = md.set(TransportKeys.ETAG, etag.strip('"'))
        if last_mod := h.get("last-modified"):
            md = md.set(HttpKeys.LAST_MODIFIED, last_mod)
        if not md.has(TransportKeys.CONTENT_TYPE) and (ct := h.get("content-type")):
            md = md.set(TransportKeys.CONTENT_TYPE, ct)
        return md.set(HttpKeys.STATUS, resp.status)


class ConfluenceContentTransport(Transport[ConfluenceRequest]):
    """
    Transport[ConfluenceRequest],
    разворачивающий 1 page-request -> page + N attachments
    """

    def __init__(
        self,
        *,
        inner: Transport[ConfluenceRequest],
        body_format: str,
        base_url: str,
        progress: IngestProgress,
        gate: AttachmentGate,
    ) -> None:
        self._inner = inner
        self._decoder = ConfluenceJsonDecoder(body_format=body_format)
        self._base_url = base_url
        self._gate = gate
        self._progress = progress

    async def close(self) -> None:
        await self._inner.close()

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        return self._inner.source_id(request)

    async def fetch(self, request: ConfluenceRequest) -> AsyncIterator[RawDocument]:
        if att := request.metadata.get(ConfluenceKeys.ATTACHMENT_INFO):
            logger.info(
                "fetch attachment start: %s [%s] %d bytes",
                att.title,
                att.media_type,
                att.file_size,
            )
            elapsed = Elapsed()
            async for raw in self._inner.fetch(request):
                yield self._watched(raw, f"attachment {att.title}")

            logger.info(
                "fetch attachment done: %s in %dms", att.title, elapsed.ms()
            )
            return

        source_id = self._inner.source_id(request)
        logger.info("fetch page start: %s", source_id)
        elapsed = Elapsed()
        async for raw in self._inner.fetch(request):
            decoded = await self._decoder.decode(raw)
            logger.info("fetch page done: %s in %dms", source_id, elapsed.ms())
            yield self._watched(decoded, f"page {source_id}")
            attachments = self._iter_attachments(
                parent=decoded,
                base_url=self._base_url,
                transport=self._inner,
                gate=self._gate,
                progress=self._progress,
            )
            async for attachment in attachments:
                yield attachment

    @staticmethod
    def _watched(raw: RawDocument, label: str) -> RawDocument:
        """Тело документа под логом: скачивание отделено от разбора."""
        return replace(raw, handle=LoggingStream(raw.handle, logger, label))

    @staticmethod
    async def _iter_attachments(
        *,
        parent: RawDocument,
        base_url: str,
        transport: Transport[ConfluenceRequest],
        progress: IngestProgress,
        gate: AttachmentGate,
    ) -> AsyncIterator[RawDocument]:
        attachments = parent.metadata.get(ConfluenceKeys.ATTACHMENTS)
        if not attachments:
            return

        # вложения идут по одному: решение, скачивание и разбор — сразу, без
        # предварительного списка, иначе страница с сотней файлов копится в памяти
        skipped: Counter[AttachmentVerdict] = Counter()
        taken = 0
        for att in attachments:
            verdict = gate.verdict(att)
            if verdict is not AttachmentVerdict.TAKE:
                skipped[verdict] += 1
                logger.info(
                    "attachment skipped (%s): id=%s title=%r media_type=%r",
                    verdict.value,
                    att.id,
                    att.title,
                    att.media_type,
                )
                continue

            taken += 1
            progress.attachments_found(1)

            req = ConfluenceRest.make_attachment_request(
                base_url=base_url,
                parent_metadata=parent.metadata,
                attachment=att,
            )
            logger.info(
                "fetch attachment start: %s [%s] %d bytes",
                att.title,
                att.media_type,
                att.file_size,
            )
            elapsed = Elapsed()
            async for raw in transport.fetch(req):
                yield ConfluenceContentTransport._watched(
                    raw, f"attachment {att.title}"
                )

            logger.info("fetch attachment done: %s in %dms", att.title, elapsed.ms())
            progress.attachment_done()

        reasons = ", ".join(f"{v.value}: {n}" for v, n in skipped.items())
        logger.info(
            "page %s: %d attachments, %d indexed%s",
            parent.source_id,
            len(attachments),
            taken,
            f" ({reasons})" if reasons else "",
        )

    @classmethod
    def from_connection(
        cls,
        conn: ConfluenceConnection,
        *,
        progress: IngestProgress,
        gate: AttachmentGate,
    ) -> ConfluenceContentTransport:
        return cls(
            inner=ConfluenceHttpTransport(CancellableHttpTransport(conn.profile)),
            body_format=conn.body_format,
            base_url=conn.base_url,
            progress=progress,
            gate=gate,
        )

    @classmethod
    async def iter_documents(
        cls,
        *,
        request_source: RequestSource[ConfluenceRequest],
        conn: ConfluenceConnection,
        progress: IngestProgress,
        gate: AttachmentGate,
    ) -> AsyncIterator[RawDocument]:
        transport = cls.from_connection(conn, progress=progress, gate=gate)
        try:
            async for request in request_source.requests():
                async for raw in transport.fetch(request):
                    yield raw
        finally:
            await transport.close()
