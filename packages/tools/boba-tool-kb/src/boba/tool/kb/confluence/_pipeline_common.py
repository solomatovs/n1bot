"""Общие стадии Confluence-pipeline'ов: Transport + JSON-Decoder + fan-out attachments.

`confluence_*_download` и `confluence_*_ingest`-тулы делят первые две
стадии — `HttpTransport` (через `conn.make_transport()`) и
`ConfluenceJsonDecoder` (JSON-payload → HTML-handle + enriched metadata).

Различаются они дальше: download читает `decoded.handle` как HTML и пишет
файл; ingest подключает Reader → Chunker → Sink через `StreamingIndexer`.

Этот модуль — единственная точка конструирования этой пары стадий.
download потребляет готовый `Iterator[RawDocument]` через
`iter_confluence_documents`. ingest берёт `transport, decoder` через
`make_confluence_stages` и передаёт их внутрь `StreamingIndexer` (тот
сам гоняет per-source iter внутри).

`iter_confluence_documents` — единственное место, где разворачивается
1 page → 1 HTML + N attachment'ов: после декодирования основной страницы
из её metadata (`ConfluenceKeys.ATTACHMENTS`) сразу же создаются
attachment-requests и тут же стримятся через тот же `HttpTransport`
(без декодера — бинарь это не JSON). Yield-порядок: сначала page,
потом её attachments по очереди. Никаких list'ов между стадиями.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from boba.indexing import PipelineContext, RawDocument, RequestSource, Transport
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources._common import make_attachment_request
from boba.transport.http import HttpRequest, HttpTransport

__all__ = ["iter_confluence_documents", "make_confluence_stages"]


def make_confluence_stages(
    conn: ConfluenceConnection,
) -> tuple[HttpTransport, ConfluenceJsonDecoder]:
    """`(transport, decoder)`-пара для Confluence-pipeline'а.

    `body_format` берётся из `conn` — единственная точка, где он связывает
    Transport и Decoder вместе.
    """
    return conn.make_transport(), ConfluenceJsonDecoder(
        body_format=conn.body_format,
    )


def iter_confluence_documents(
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    pctx: PipelineContext,
) -> Iterator[RawDocument]:
    """Стрим `RawDocument` из Confluence: source → transport → decoder (+attachments).

    Per page-request: 1 HTTP → 1 JSON → 1 page-`RawDocument` (HTML-handle,
    metadata page_id/host/version/title/space_key/ancestors + список
    `ATTACHMENTS`), затем для каждой записи из `ATTACHMENTS` — 1 HTTP →
    1 attachment-`RawDocument` (binary handle, metadata `ATTACHMENT_INFO` +
    унаследованные от родителя `PAGE_ID`/`HOST`/`SPACE_KEY`/`ANCESTORS_TITLES`).

    Yield-контракт: page yield'ится первой, потом её attachments по одному —
    consumer должен полностью прочитать handle текущего документа до того,
    как пулить следующий (стандартный streaming-Transport-контракт).

    `httpx.HTTPError` пробрасывается наверх — caller сам решает, как
    оборачивать (download → RuntimeError, ingest → StreamingIndexer
    через `IndexingError`). Ошибка скачивания одного attachment'а валит
    всю страницу; per-attachment skip — отдельная политика, сейчас не реализована.
    """
    transport, decoder = make_confluence_stages(conn)
    auth = conn.make_auth()
    for http_req in request_source.stream(pctx):
        for raw in transport.stream(pctx, [http_req]):
            decoded = decoder.convert(raw)
            yield decoded
            yield from _iter_attachments(
                parent=decoded,
                base_url=conn.base_url,
                auth=auth,
                transport=transport,
                pctx=pctx,
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
