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
from collections.abc import Iterable, Iterator

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.models import (
    AttachmentFilter,
    ConfluenceKeys,
    HttpKeys,
)
from boba.chainlit2.agent.tools.confluence.parsing import ConfluenceJsonDecoder
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluenceRequest,
    ConfluenceRest,
)
from boba.chainlit2.agent.tools.http import CancellableHttpTransport
from boba.indexing import (
    Metadata,
    RawDocument,
    RequestSource,
    SourceId,
    Transport,
    TransportKeys,
)
from boba.transport.http import HttpResponse, HttpTransport

logger = logging.getLogger(__name__)

__all__ = ["ConfluenceContentTransport", "ConfluenceHttpTransport"]


class ConfluenceHttpTransport(Transport[ConfluenceRequest]):
    """ConfluenceRequest -> RawDocument: чистый HTTP + обогащение metadata.

    Оборачивает чистый HttpTransport: исполняет request.http, собирает
    RawDocument (source_id выводит сам из реально запрашиваемого URL —
    resolve_url без query; metadata + ключи из заголовков ответа).
    Lifecycle handle — у HttpTransport.fetch: поток открыт пока идёт итерация
    результата, закроется на выходе из этого generator'а.
    """

    def __init__(self, http: HttpTransport) -> None:
        self._http = http

    def close(self) -> None:
        self._http.close()

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        resolved = self._http.resolve_url(request.http)
        return SourceId(resolved.split("?", 1)[0])

    def fetch(self, request: ConfluenceRequest) -> Iterable[RawDocument]:
        with self._http.fetch(request.http) as resp:
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
        attachment_filter: AttachmentFilter | None = None,
    ) -> None:
        self._inner = inner
        self._decoder = ConfluenceJsonDecoder(body_format=body_format)
        self._base_url = base_url
        self._attachment_filter = attachment_filter or AttachmentFilter()

    def close(self) -> None:
        self._inner.close()

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        return self._inner.source_id(request)

    def fetch(self, request: ConfluenceRequest) -> Iterable[RawDocument]:
        if request.metadata.has(ConfluenceKeys.ATTACHMENT_INFO):
            yield from self._inner.fetch(request)
            return
        for raw in self._inner.fetch(request):
            decoded = self._decoder.decode(raw)
            yield decoded
            yield from self._iter_attachments(
                parent=decoded,
                base_url=self._base_url,
                transport=self._inner,
                att_filter=self._attachment_filter,
            )

    @staticmethod
    def _iter_attachments(
        *,
        parent: RawDocument,
        base_url: str,
        transport: Transport[ConfluenceRequest],
        att_filter: AttachmentFilter | None = None,
    ) -> Iterator[RawDocument]:
        attachments = parent.metadata.get(ConfluenceKeys.ATTACHMENTS)
        if not attachments:
            return
        flt = att_filter or AttachmentFilter()
        for att in attachments:
            if not flt.matches(att):
                logger.debug(
                    "attachment skipped by filter: id=%s title=%r media_type=%r",
                    att.id,
                    att.title,
                    att.media_type,
                )
                continue
            req = ConfluenceRest.make_attachment_request(
                base_url=base_url,
                parent_metadata=parent.metadata,
                attachment=att,
            )
            yield from transport.fetch(req)

    @classmethod
    def from_connection(
        cls,
        conn: ConfluenceConnection,
        *,
        attachment_filter: AttachmentFilter | None = None,
    ) -> ConfluenceContentTransport:
        return cls(
            inner=ConfluenceHttpTransport(CancellableHttpTransport(conn.profile)),
            body_format=conn.body_format,
            base_url=conn.base_url,
            attachment_filter=attachment_filter,
        )

    @classmethod
    def iter_documents(
        cls,
        *,
        request_source: RequestSource[ConfluenceRequest],
        conn: ConfluenceConnection,
        attachment_filter: AttachmentFilter | None = None,
    ) -> Iterator[RawDocument]:
        transport = cls.from_connection(conn, attachment_filter=attachment_filter)
        try:
            for request in request_source.requests():
                yield from transport.fetch(request)
        finally:
            transport.close()
