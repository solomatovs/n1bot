"""Транспорт Confluence для конвейера: HTTP, разбор JSON страницы, спул вложений.

ConfluenceHttpTransport исполняет чистый HTTP-запрос и собирает RawDocument
с source_id по URL. ConfluenceSourceTransport поверх него различает два вида
запросов, которые даёт ConfluenceDiscovery:

1. Страница: JSON тела -> ConfluenceJsonDecoder -> HTML-handle с хэшем тела.
2. Вложение (в metadata есть ConfluenceKeys.ATTACHMENT_INFO): тело льётся во
   временный файл с подсчётом sha256 по дороге, наружу уходит SpooledBody с
   хэшем; файл живёт, пока идёт итерация fetch.

Хэш тела (TransportKeys.BODY_HASH) конвейер сверяет с реестром и не разбирает
то, что уже разбирал.

Ошибки:
TransportError — Confluence недоступен, ответил статусом или оборвал тело;
    ошибки httpx наружу не выходят.
ConfluencePayloadError — тело страницы не разбирается как JSON Confluence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx

from boba.indexing import (
    AsyncBinaryStream,
    Metadata,
    RawDocument,
    SourceId,
    SpooledBody,
    Transport,
    TransportError,
    TransportKeys,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    ConfluenceKeys,
    ConfluenceSourceId,
    HttpKeys,
)
from boba.tool.kb.confluence.parsing import BodyDigest, ConfluenceJsonDecoder
from boba.tool.kb.confluence.request_sources import ConfluenceRequest
from boba.tool.kb.indexing_log import LoggingStream
from boba.toolkit.timing import Elapsed
from boba.transport.http import (
    CancellableHttpTransport,
    HttpResponse,
    HttpTransport,
)

logger = logging.getLogger(__name__)

__all__ = ["ConfluenceHttpTransport", "ConfluenceSourceTransport"]


class GuardedStream(AsyncBinaryStream):
    """Тело ответа под контрактом слоя: httpx-обрыв уходит наверх TransportError."""

    def __init__(self, inner: AsyncBinaryStream, source_id: SourceId) -> None:
        self._inner = inner
        self._source_id = source_id

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                yield chunk
        except httpx.HTTPError as exc:
            raise self._failed(exc) from exc

    async def read(self) -> bytes:
        try:
            return await self._inner.read()
        except httpx.HTTPError as exc:
            raise self._failed(exc) from exc

    def _failed(self, exc: httpx.HTTPError) -> TransportError:
        msg = (
            f"reading the response body of confluence {self._source_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return TransportError(msg)


class ConfluenceHttpTransport(Transport[ConfluenceRequest]):
    """ConfluenceRequest -> RawDocument: чистый HTTP + обогащение metadata.

    Оборачивает чистый HttpTransport: исполняет request.http, собирает
    RawDocument (source_id по реально запрашиваемому URL без query; metadata +
    ключи из заголовков ответа). Handle живёт, пока идёт итерация результата.
    """

    def __init__(self, http: HttpTransport) -> None:
        self._http = http

    async def close(self) -> None:
        await self._http.close()

    def source_id(self, request: ConfluenceRequest) -> SourceId:
        """URL запроса без query и фрагмента: один объект — один id."""
        return ConfluenceSourceId.of_url(self._http.resolve_url(request.http))

    async def fetch(self, request: ConfluenceRequest) -> AsyncIterator[RawDocument]:
        source_id = self.source_id(request)
        try:
            async with self._http.fetch(request.http) as resp:
                yield RawDocument(
                    handle=GuardedStream(resp.stream, source_id),
                    source_id=source_id,
                    metadata=self._enrich(request.metadata, resp),
                )
        except httpx.HTTPError as exc:
            msg = f"GET confluence {source_id}: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

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


class ConfluenceSourceTransport(Transport[ConfluenceRequest]):
    """Transport[ConfluenceRequest] для конвейера: страница разбирается из JSON,
    вложение спулится на диск; у обоих в metadata хэш тела."""

    def __init__(
        self,
        *,
        inner: Transport[ConfluenceRequest],
        decoder: ConfluenceJsonDecoder,
    ) -> None:
        self._inner = inner
        self._decoder = decoder

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
                async for spooled in self._spooled(raw, att.title):
                    yield spooled

            logger.info("fetch attachment done: %s in %dms", att.title, elapsed.ms())
            return

        source_id = self._inner.source_id(request)
        logger.info("fetch page start: %s", source_id)
        elapsed = Elapsed()
        async for raw in self._inner.fetch(request):
            decoded = await self._decoder.decode(raw)
            logger.info("fetch page done: %s in %dms", source_id, elapsed.ms())
            yield replace(
                decoded,
                handle=LoggingStream(decoded.handle, logger, f"page {source_id}"),
            )

    @staticmethod
    async def _spooled(raw: RawDocument, title: str) -> AsyncIterator[RawDocument]:
        """Тело во временный файл с суффиксом имени вложения и sha256 по дороге."""
        suffix = Path(title).suffix
        fd, name = tempfile.mkstemp(suffix=suffix, prefix="confluence-")
        path = Path(name)
        digest = BodyDigest.new()
        try:
            with os.fdopen(fd, "wb") as spool:
                async for chunk in raw.handle:
                    digest.update(chunk)
                    await asyncio.to_thread(spool.write, chunk)

            body_hash = digest.hexdigest()
            logger.info(
                "spooled %s: %d bytes, sha256 %s",
                title,
                path.stat().st_size,
                body_hash[:12],
            )
            yield replace(
                raw,
                handle=SpooledBody(path),
                metadata=raw.metadata.set(TransportKeys.BODY_HASH, body_hash),
            )
        finally:
            path.unlink(missing_ok=True)

    @classmethod
    def from_connection(cls, conn: ConfluenceConnection) -> ConfluenceSourceTransport:
        http = CancellableHttpTransport(conn.profile)
        return cls(
            inner=ConfluenceHttpTransport(http),
            decoder=ConfluenceJsonDecoder(
                profile=conn.profile,
                body_format=conn.body_format,
            ),
        )
