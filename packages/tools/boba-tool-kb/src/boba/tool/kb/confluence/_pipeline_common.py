"""Общие стадии Confluence-pipeline'ов: Transport + JSON-Decoder.

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
"""

from __future__ import annotations

from collections.abc import Iterator

from boba.indexing import PipelineContext, RawDocument, RequestSource
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
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
    """Стрим `RawDocument` из Confluence: source → transport → decoder.

    Per-request: 1 HTTP → 1 JSON → 1 `RawDocument` с HTML-handle и
    обогащённой metadata (page_id/host из request'а, version/title/
    space_key/ancestors из JSON, etag/last_modified из ответа).

    `httpx.HTTPError` пробрасывается наверх — caller сам решает, как
    оборачивать (download → RuntimeError, ingest → StreamingIndexer
    через `IndexingError`).
    """
    transport, decoder = make_confluence_stages(conn)
    for http_req in request_source.stream(pctx):
        for raw in transport.stream(pctx, [http_req]):
            yield decoder.convert(raw)
