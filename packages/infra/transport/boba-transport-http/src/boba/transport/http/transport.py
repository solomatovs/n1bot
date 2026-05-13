"""HttpTransport: HttpRequest → RawDocument через httpx.Client.

Контракт lifecycle handle: для каждого `HttpRequest` делает `httpx.Client`-
запрос ВНУТРИ generator'а, открывает stream-response через `with`, yield'ит
RawDocument с handle. После возврата control'а в generator (Reader прочитал)
with-блок закрывает response — ровно как и должно быть для streaming.

`auth: AuthApplier` вызывается на kwargs httpx.Client'а — Transport не
знает про PAT/Basic/OAuth, просто применяет callback.

Пробрасывает `source_id` и `metadata` Request'а в RawDocument, добавляя
свои метаданные (etag, content_type, last_modified, status) через
`TransportKeys`/`HttpKeys`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

import httpx

from boba.indexing import (
    PipelineContext,
    RawDocument,
    Transport,
    TransportKeys,
)
from boba.transport.http.keys import HttpKeys
from boba.transport.http.request import HttpRequest

__all__ = ["HttpTransport"]


class HttpTransport(Transport[HttpRequest]):
    """
    `Transport[HttpRequest]`: HTTP-запрос → `RawDocument` со streaming-response handle.

    **Схема**:
    ```python
    HttpRequest   ──────────────────HttpTransport.stream──→  RawDocument
        url         : str           ┐
        method      : str           ├──httpx.stream──→
        headers     : Mapping       │  (auth применяется к client kwargs)
        auth        : AuthApplier   ┘
        source_id   : SourceId   ──pass─────────────→            source_id   (тот же)
        metadata    : Metadata   ──merge────────────→            metadata    (+ TransportKeys.ETAG / CONTENT_TYPE, HttpKeys.LAST_MODIFIED, HttpKeys.STATUS)
                                                          →      handle      : _ResponseHandle  (BinaryStream-адаптер; закроется по выходу из stream)
    ```

    **Lifecycle handle**:
    ```python
    with httpx.Client(**kw) as client, client.stream(method, url) as resp:
        yield RawDocument(handle=_ResponseHandle(resp), ...)
    # сюда — после next(generator); resp/client закрыты с выходом из with
    ```
    `_ResponseHandle` реализует `BinaryStream` поверх `resp.iter_bytes()`,
    чтобы Reader мог звать `handle.read()` единообразно.

    **Поведение на ошибки**:
    - 4xx/5xx → `httpx.HTTPStatusError` через `raise_for_status()` (не глушится).
    - timeout/connection — стандартные `httpx`-исключения.

    `auth: AuthApplier` — callback на client kwargs; Transport не знает,
    что внутри (PAT, Basic, Bearer, OAuth), просто применяет.

    **Пример**:
    ```python
    transport = HttpTransport(timeout_sec=30.0, verify=True)
    requests = iter([
        HttpRequest(
            url="https://api.example.com/doc/1",
            method="GET",
            headers={"Accept": "text/html"},
            source_id=SourceId("https://api.example.com/doc/1"),
        ),
    ])

    # 1 HttpRequest → 1 RawDocument; handle потоковый, закроется после next(...).
    raw = next(iter(transport.stream(ctx, requests)))
    raw == RawDocument(
        handle=<_ResponseHandle resp=200 OK>,                          # новое: streaming-адаптер resp.iter_bytes()
        source_id=SourceId("https://api.example.com/doc/1"),           # pass из HttpRequest
        metadata=(                                                     # merge: + 4 ключа из HTTP-заголовков
            Metadata.empty()
            .set(TransportKeys.ETAG, "abc-123")                        # новое: response ETag (без кавычек)
            .set(TransportKeys.CONTENT_TYPE, "text/html; charset=utf-8")  # новое: Content-Type
            .set(HttpKeys.LAST_MODIFIED, "Wed, 21 Oct 2026 07:28:00 GMT")  # новое: Last-Modified
            .set(HttpKeys.STATUS, 200)                                 # новое: HTTP-код ответа
        ),
    )
    raw.handle.read()  # → b"<html>..."
    ```
    """  # noqa: E501

    DEFAULT_TIMEOUT_SEC: ClassVar[float] = 30.0

    def __init__(
        self,
        *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        verify: bool = False,
    ) -> None:
        self._timeout = timeout_sec
        self._verify = verify

    def name(self) -> str:
        return "HttpTransport"

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[HttpRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            yield from self._fetch_one(req)

    def _fetch_one(self, req: HttpRequest) -> Iterable[RawDocument]:
        with (
            httpx.Client(
                timeout=self._timeout,
                verify=self._verify,
                headers=dict(req.headers) if req.headers else {},
                auth=req.auth,
            ) as client,
            client.stream(req.method, req.url) as resp,
        ):
            resp.raise_for_status()
            yield RawDocument(
                handle=_ResponseHandle(resp),
                source_id=req.source_id,
                metadata=_enrich_metadata(req.metadata, resp),
            )


def _enrich_metadata(base, resp: httpx.Response):
    """HTTP-заголовки → metadata для skip-if-unchanged + диагностики."""
    md = base
    h = resp.headers
    if etag := h.get("etag"):
        md = md.set(TransportKeys.ETAG, etag.strip('"'))
    if last_mod := h.get("last-modified"):
        md = md.set(HttpKeys.LAST_MODIFIED, last_mod)
    if ct := h.get("content-type"):
        md = md.set(TransportKeys.CONTENT_TYPE, ct)
    md = md.set(HttpKeys.STATUS, resp.status_code)
    return md


class _ResponseHandle:
    """Адаптер httpx.Response.iter_bytes → BinaryIO (read/readable)."""

    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp
        self._buffer = b""
        self._iter = resp.iter_bytes()
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunks = [self._buffer]
            self._buffer = b""
            for chunk in self._iter:
                chunks.append(chunk)
            self._eof = True
            return b"".join(chunks)
        while len(self._buffer) < n and not self._eof:
            try:
                self._buffer += next(self._iter)
            except StopIteration:
                self._eof = True
                break
        out = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return out

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        # Закрытие — обязанность Transport'а через with-block над response.
        pass

    @property
    def closed(self) -> bool:
        return self._eof and not self._buffer
