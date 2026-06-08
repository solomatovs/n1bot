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

from boba.indexing import (
    Metadata,
    RawDocument,
    RequestSource,
    Transport,
    TransportKeys,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter, ConfluenceKeys, HttpKeys
from boba.tool.kb.confluence.parsing import ConfluenceJsonDecoder
from boba.tool.kb.confluence.request_sources import ConfluenceRequest, ConfluenceRest
from boba.transport.http import HttpResponse, HttpTransport

logger = logging.getLogger(__name__)

__all__ = ["ConfluenceContentTransport", "ConfluenceHttpTransport"]


class ConfluenceHttpTransport(Transport[ConfluenceRequest]):
    """ConfluenceRequest -> RawDocument: чистый HTTP + обогащение metadata.

    Оборачивает чистый HttpTransport: исполняет request.http, собирает
    RawDocument (source_id из request, metadata + ключи из заголовков ответа).
    Lifecycle handle — у HttpTransport.fetch: поток открыт пока идёт итерация
    результата, закроется на выходе из этого generator'а.
    """

    def __init__(self, http: HttpTransport) -> None:
        self._http = http

    def close(self) -> None:
        self._http.close()

    def fetch(self, request: ConfluenceRequest) -> Iterable[RawDocument]:
        with self._http.fetch(request.http) as resp:
            yield RawDocument(
                handle=resp.stream,
                source_id=request.source_id,
                metadata=self._enrich(request.metadata, resp),
            )

    @staticmethod
    def _enrich(base: Metadata, resp: HttpResponse) -> Metadata:
        """HTTP-заголовки -> metadata для skip-if-unchanged + диагностики."""
        md = base
        h = resp.headers
        if etag := h.get("etag"):
            md = md.set(TransportKeys.ETAG, etag.strip('"'))
        if last_mod := h.get("last-modified"):
            md = md.set(HttpKeys.LAST_MODIFIED, last_mod)
        if ct := h.get("content-type"):
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
        """Yield бинарные RawDocument'ы для каждого вложения родительской страницы.

        Не накапливает: один attachment-request -> один прогон через transport
        -> один yield. Если у страницы нет вложений (нет ATTACHMENTS в meta) —
        ничего не yield'ит. Декодер по бинарям НЕ запускается — Confluence
        отдаёт raw bytes с правильным Content-Type в response-header,
        ConfluenceHttpTransport кладёт его в TransportKeys.CONTENT_TYPE.

        att_filter (если задан и непустой) применяется ДО HTTP-запроса —
        не прошедшие фильтр attachment'ы не запрашиваются вовсе. Пустой
        фильтр (или None) пропускает все вложения, как раньше.

        @staticmethod (а не метод инстанса): тесты драйвят его напрямую с
        фейк-транспортом, без необходимости поднимать весь ConfluenceContentTransport.
        """
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
        """Factory: ConfluenceConnection -> готовый unified transport.

        Единственная точка, где body_format/base_url из conn собираются в
        один Transport (auth уже внутри HttpTransport через conn.profile).
        И download, и ingest должны конструировать transport через эту фабрику —
        чтобы fan-out + decode были общими между ними.

        attachment_filter (опциональный) фильтрует attachment-fan-out:
        непрошедшие даже не запрашиваются по HTTP. По умолчанию — passthrough.
        """
        return cls(
            inner=ConfluenceHttpTransport(HttpTransport(conn.profile)),
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
        """Стрим RawDocument из Confluence: source -> ConfluenceContentTransport.

        Per page-request: 1 HTTP-JSON -> 1 декодированная HTML-RawDocument +
        N attachment-RawDocument'ов (по _links.download каждого вложения,
        с media_type из ответа в TransportKeys.CONTENT_TYPE).

        Yield-контракт: page yield'ится первой, потом её attachments по одному —
        consumer должен полностью прочитать handle текущего документа до того,
        как пулить следующий.

        attachment_filter (если задан) сужает поток вложений на fan-out
        стадии — отсеянные attachment'ы не приходят даже как HTTP-запрос.

        httpx.HTTPError пробрасывается наверх — caller сам решает, как
        оборачивать (download -> RuntimeError, ingest -> Pipeline
        через IndexingError).
        """
        transport = cls.from_connection(conn, attachment_filter=attachment_filter)
        try:
            for request in request_source.requests():
                yield from transport.fetch(request)
        finally:
            transport.close()
