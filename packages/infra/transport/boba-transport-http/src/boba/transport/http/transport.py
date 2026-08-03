"HttpTransport: HttpProfile + HttpRequest -> HttpResponse через httpx.Client"

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from boba.transport.http.connection import HttpProfile
from boba.transport.http.request import HttpRequest
from boba.transport.http.response import HttpResponse

__all__ = ["HttpTransport"]

logger = logging.getLogger(__name__)


class HttpTransport:
    """Исполняет HttpRequest через httpx.Client, которым владеет (close/with).

    Retry покрывает соединение/заголовки/статус; обрыв чтения тела не ретраится.
    """

    def __init__(self, profile: HttpProfile) -> None:
        self._profile = profile
        # headers/params на клиент не кладём: они целиком per-request
        self._client = httpx.Client(
            base_url=profile.base_url or "",
            timeout=profile.timeout_sec,
            verify=profile.ssl_verify,
            auth=profile.auth.httpx_auth(),
        )

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, request: HttpRequest) -> str:
        "Абсолютный URL (base_url + url + params) без сетевого вызова"
        return str(
            self._client.build_request(
                request.method,
                request.url,
                # пустой params-dict в httpx срезает ?query из url — отдаём None
                params=request.params or None,
            ).url,
        )

    @contextmanager
    def fetch(self, request: HttpRequest) -> Iterator[HttpResponse]:
        """Открыть запрос с retry и отдать HttpResponse; тело закроется на выходе."""
        resp = self._open_with_retry(request)
        try:
            yield HttpResponse(
                status=resp.status_code,
                headers=dict(resp.headers),
                stream=_ResponseHandle(resp),
            )
        finally:
            resp.close()

    def _open_with_retry(self, request: HttpRequest) -> httpx.Response:
        """Соединение + заголовки + статус; retry на 5xx/transport-ошибках."""
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, self._profile.retry_attempts + 1):
            resp: httpx.Response | None = None
            try:
                resp = self._client.send(
                    self._client.build_request(
                        request.method,
                        request.url,
                        headers=request.headers,
                        # см. resolve_url: пустой dict обнуляет url-query в httpx.
                        params=request.params or None,
                        content=request.content,
                        data=request.data,
                        files=request.files,
                        json=request.json,
                    ),
                    stream=True,
                )
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                if resp is not None:
                    resp.close()

                if not e.response.is_server_error:  # 4xx — не ретраим
                    raise

                last_exc = e
            except httpx.TransportError as e:  # timeout / connect — transient
                if resp is not None:
                    resp.close()
                last_exc = e
            self._backoff(attempt, request, last_exc)
        if last_exc is None:  # недостижимо: цикл либо вернул, либо выставил last_exc
            msg = f"HTTP {request.method} {request.url}: неизвестная ошибка"
            raise httpx.HTTPError(msg)
        raise last_exc

    def _backoff(
        self,
        attempt: int,
        request: HttpRequest,
        exc: httpx.HTTPError | None,
    ) -> None:
        """Линейный backoff между попытками; на последней попытке не спит."""
        if attempt >= self._profile.retry_attempts:
            return
        delay = self._profile.retry_backoff_sec * attempt
        logger.warning(
            "HTTP %s %s неудачно (%s); retry %d/%d через %.1fs",
            request.method,
            request.url,
            type(exc).__name__,
            attempt,
            self._profile.retry_attempts,
            delay,
        )
        time.sleep(delay)


class _ResponseHandle:
    """Адаптер httpx.Response.iter_bytes -> ByteStream (read)."""

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
        # Закрытие ответа — обязанность HttpTransport.fetch (finally).
        pass

    @property
    def closed(self) -> bool:
        return self._eof and not self._buffer
