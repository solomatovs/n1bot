"""HttpTransport: HttpRequest → RawDocument через httpx.Client.

Контракт lifecycle handle: для каждого HttpRequest делает `httpx.Client`-
запрос ВНУТРИ generator'а, открывает stream-response через `with`, yield'ит
RawDocument с handle. После возврата control'а в generator (Reader прочитал)
with-блок закрывает response — ровно как и должно быть для streaming.

`auth: AuthApplier` вызывается на kwargs httpx.Client'а — Transport не
знает про PAT/Basic/OAuth, просто применяет callback.

Пробрасывает `source_id` и `metadata` Request'а в RawDocument, добавляя
свои метаданные (etag, content_type, last_modified, status).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from boba.http_transport.request import HttpRequest
from boba.indexing import IndexingContext, RawDocument, Transport

__all__ = ["HttpTransport"]

_DEFAULT_TIMEOUT = 30.0


class HttpTransport(Transport[HttpRequest]):
    """httpx-based generic HTTP transport."""

    def __init__(
        self,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT,
        verify: bool = True,
    ) -> None:
        self._timeout = timeout_sec
        self._verify = verify

    def name(self) -> str:
        return "HttpTransport"

    def stream(
        self,
        ctx: IndexingContext,
        stream: Iterable[HttpRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            yield from self._fetch_one(req)

    def _fetch_one(self, req: HttpRequest) -> Iterable[RawDocument]:
        client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "verify": self._verify,
            "headers": dict(req.headers) if req.headers else {},
        }
        if req.auth is not None:
            req.auth(client_kwargs)

        if not req.source_id:
            msg = (
                "HttpRequest.source_id must be set by RequestSource — "
                "Transport не формирует identity, только исполняет запрос. "
                f"url={req.url!r}"
            )
            raise ValueError(msg)
        with (
            httpx.Client(**client_kwargs) as client,
            client.stream(req.method, req.url) as resp,
        ):
            resp.raise_for_status()
            merged_meta: dict[str, str] = {**req.metadata}
            _enrich_with_response(merged_meta, resp)
            yield RawDocument(
                handle=_ResponseHandle(resp),
                source_id=req.source_id,
                content_hint=resp.headers.get("content-type", ""),
                metadata=merged_meta,
            )


def _enrich_with_response(md: dict[str, str], resp: httpx.Response) -> None:
    """HTTP-заголовки → metadata для skip-if-unchanged + диагностики."""
    h = resp.headers
    if etag := h.get("etag"):
        md["etag"] = etag.strip('"')
    if last_mod := h.get("last-modified"):
        md.setdefault("last_modified", last_mod)
    if ct := h.get("content-type"):
        md.setdefault("content_type", ct)
    md.setdefault("status", str(resp.status_code))


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
