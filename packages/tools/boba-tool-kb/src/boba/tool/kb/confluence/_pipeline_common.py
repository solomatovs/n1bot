"""Общий Confluence-pipeline стейдж: HTTP + JSON-decode + attachment fan-out.

`ConfluenceContentTransport` — единый `Transport[HttpRequest]`, через который
ходят и download, и ingest. Внутри:

1. Если `HttpRequest.metadata` содержит `ConfluenceKeys.ATTACHMENT_INFO` —
   это уже attachment-request (его сгенерировал сам transport на предыдущей
   итерации page-request'а). Прозрачно делегируется во внутренний
   `HttpTransport`, без декодирования.
2. Иначе — page-request: внутренний transport отдаёт JSON →
   `ConfluenceJsonDecoder` извлекает HTML и обогащает metadata (включая
   `ConfluenceKeys.ATTACHMENTS` и обновлённый `TransportKeys.CONTENT_TYPE`
   = `text/html`) → yield декодированной страницы → для каждого вложения
   из `ATTACHMENTS` строится attachment-`HttpRequest` через
   `make_attachment_request` и тоже стримится через внутренний transport.

Yield-порядок per page-request: сам page (HTML), потом вложения в порядке
из Confluence JSON. Никаких list'ов между стадиями: stream-pipeline через
yield/yield from.

Download потребляет результат через `iter_confluence_documents`-обёртку,
ingest подключает `ConfluenceContentTransport` напрямую в `StreamingIndexer`
(с `decoders=()` — декодинг уже выполнен внутри transport'а; reader должен
быть `DispatchReader`, потому что поток смешанный: HTML + произвольные
attachment-media-types).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import httpx

from boba.indexing import PipelineContext, RawDocument, RequestSource, Transport
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources._common import make_attachment_request
from boba.transport.http import HttpRequest

__all__ = [
    "ConfluenceContentTransport",
    "iter_confluence_documents",
    "make_confluence_transport",
]


class ConfluenceContentTransport(Transport[HttpRequest]):
    """`Transport[HttpRequest]`, разворачивающий 1 page-request → page + N attachments.

    Один экземпляр держит per-`conn`-конфигурацию (base_url, auth, body_format)
    плюс внутренний `HttpTransport`. Reuse между несколькими `.stream()`-вызовами
    безопасен — внутри нет mutable state.
    """

    def __init__(
        self,
        *,
        inner: Transport[HttpRequest],
        body_format: str,
        base_url: str,
        auth: httpx.Auth | None,
    ) -> None:
        self._inner = inner
        self._decoder = ConfluenceJsonDecoder(body_format=body_format)
        self._base_url = base_url
        self._auth = auth

    def name(self) -> str:
        return "ConfluenceContentTransport"

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[HttpRequest],
    ) -> Iterable[RawDocument]:
        for req in stream:
            if req.metadata.has(ConfluenceKeys.ATTACHMENT_INFO):
                yield from self._inner.stream(ctx, [req])
                continue
            for raw in self._inner.stream(ctx, [req]):
                decoded = self._decoder.convert(raw)
                yield decoded
                yield from _iter_attachments(
                    parent=decoded,
                    base_url=self._base_url,
                    auth=self._auth,
                    transport=self._inner,
                    pctx=ctx,
                )


def _iter_attachments(
    *,
    parent: RawDocument,
    base_url: str,
    auth: httpx.Auth | None,
    transport: Transport[HttpRequest],
    pctx: PipelineContext,
) -> Iterator[RawDocument]:
    """Yield бинарные `RawDocument`'ы для каждого вложения родительской страницы.

    Не накапливает: один attachment-request → один прогон через transport
    → один yield. Если у страницы нет вложений (нет `ATTACHMENTS` в meta) —
    ничего не yield'ит. Декодер по бинарям НЕ запускается — Confluence
    отдаёт raw bytes с правильным `Content-Type` в response-header,
    HttpTransport кладёт его в `TransportKeys.CONTENT_TYPE`.

    Free-function (а не метод): тесты из шага 2 драйвят его напрямую с
    фейк-транспортом, без необходимости поднимать весь `ConfluenceContentTransport`.
    """
    attachments = parent.metadata.get(ConfluenceKeys.ATTACHMENTS)
    if not attachments:
        return
    for att in attachments:
        req = make_attachment_request(
            base_url=base_url,
            auth=auth,
            parent_metadata=parent.metadata,
            attachment=att,
        )
        yield from transport.stream(pctx, [req])


def make_confluence_transport(conn: ConfluenceConnection) -> ConfluenceContentTransport:
    """Factory: `ConfluenceConnection` → готовый unified transport.

    Единственная точка, где `body_format`/`base_url`/`auth`-параметры из
    `conn` собираются в один Transport. И download, и ingest должны
    конструировать transport через эту функцию — чтобы fan-out + decode
    были общими между ними.
    """
    return ConfluenceContentTransport(
        inner=conn.make_transport(),
        body_format=conn.body_format,
        base_url=conn.base_url,
        auth=conn.make_auth(),
    )


def iter_confluence_documents(
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    pctx: PipelineContext,
) -> Iterator[RawDocument]:
    """Стрим `RawDocument` из Confluence: source → ConfluenceContentTransport.

    Per page-request: 1 HTTP-JSON → 1 декодированная HTML-`RawDocument` +
    N attachment-`RawDocument`'ов (по `_links.download` каждого вложения,
    с media_type из ответа в `TransportKeys.CONTENT_TYPE`).

    Yield-контракт: page yield'ится первой, потом её attachments по одному —
    consumer должен полностью прочитать handle текущего документа до того,
    как пулить следующий.

    `httpx.HTTPError` пробрасывается наверх — caller сам решает, как
    оборачивать (download → RuntimeError, ingest → StreamingIndexer
    через `IndexingError`).
    """
    transport = make_confluence_transport(conn)
    for http_req in request_source.stream(pctx):
        yield from transport.stream(pctx, [http_req])
