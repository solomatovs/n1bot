"""HttpTransport: HttpProfile + HttpRequest -> HttpResponse через httpx.AsyncClient.

Транспорт только асинхронный: конвейер индексации ведёт несколько источников
сразу, и синхронного варианта, который занимал бы поток на время сетевого
ожидания, здесь нет.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from boba.cancellation import current_cancellation
from boba.transport.http.connection import HttpProfile

__all__ = [
    "ByteStream",
    "CancellableHttpTransport",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "ResponseStream",
    "RetryPolicy",
]

logger = logging.getLogger(__name__)


class RetryPolicy:
    """Политика повторов: 5xx и transport-ошибки повторяются, 4xx — нет."""

    def __init__(self, profile: HttpProfile) -> None:
        self._attempts = profile.retry_attempts
        self._backoff = profile.retry_backoff_sec

    @property
    def attempts(self) -> int:
        return self._attempts

    @staticmethod
    def retryable(exc: httpx.HTTPError) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.is_server_error
        return isinstance(exc, httpx.TransportError)

    def more(self, attempt: int) -> bool:
        """Остались ли попытки после attempt."""
        return attempt < self._attempts

    def delay(self, attempt: int) -> float:
        """Линейный backoff между попытками."""
        return self._backoff * attempt

    def log(self, attempt: int, request: HttpRequest, exc: httpx.HTTPError) -> None:
        logger.warning(
            "HTTP %s %s неудачно (%s); retry %d/%d через %.1fs",
            request.method,
            request.url,
            type(exc).__name__,
            attempt,
            self._attempts,
            self.delay(attempt),
        )

    @staticmethod
    def exhausted(request: HttpRequest) -> httpx.HTTPError:
        """Недостижимая ветка: цикл либо вернул ответ, либо запомнил ошибку."""
        return httpx.HTTPError(
            f"HTTP {request.method} {request.url}: неизвестная ошибка",
        )


class HttpTransport:
    """Исполняет HttpRequest через httpx.AsyncClient, которым владеет.

    Retry покрывает соединение, заголовки и статус; обрыв чтения тела не
    ретраится. Тело отдаётся потоком и живёт, пока открыт блок fetch.
    """

    def __init__(self, profile: HttpProfile) -> None:
        self._profile = profile
        self._retry = RetryPolicy(profile)
        # headers/params на клиент не кладём: они целиком per-request
        self._client = httpx.AsyncClient(
            base_url=profile.base_url or "",
            timeout=profile.timeout_sec,
            verify=profile.ssl_verify,
            auth=profile.httpx_auth(),
        )

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

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

    @asynccontextmanager
    async def fetch(self, request: HttpRequest) -> AsyncGenerator[HttpResponse, None]:
        """Открыть запрос с retry; тело живёт потоком до выхода из блока."""
        resp = await self._open_with_retry(request)
        try:
            yield HttpResponse(
                status=resp.status_code,
                headers=dict(resp.headers),
                stream=ResponseStream(resp),
            )
        finally:
            await resp.aclose()

    async def _open_with_retry(self, request: HttpRequest) -> httpx.Response:
        """Соединение + заголовки + статус; retry на 5xx/transport-ошибках."""
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, self._retry.attempts + 1):
            resp: httpx.Response | None = None
            try:
                resp = await self._client.send(self._build(request), stream=True)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as e:
                if resp is not None:
                    await resp.aclose()
                if not self._retry.retryable(e):
                    raise
                last_exc = e
                if not self._retry.more(attempt):
                    break
                self._retry.log(attempt, request, e)
                await asyncio.sleep(self._retry.delay(attempt))
        if last_exc is None:
            raise RetryPolicy.exhausted(request)
        raise last_exc

    def _build(self, request: HttpRequest) -> httpx.Request:
        return self._client.build_request(
            request.method,
            request.url,
            headers=request.headers,
            # см. resolve_url: пустой dict обнуляет url-query в httpx
            params=request.params or None,
            content=request.content,
            data=request.data,
            files=request.files,
            json=request.json,
        )


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса; body-поля уходят в httpx build_request как есть.

    Retry реплеит in-memory body и seekable-files; генератор content одноразов.
    """

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    content: bytes | str | Iterable[bytes] | None = None
    data: Mapping[str, Any] | None = None
    files: Any | None = None
    json: Any | None = None


class ByteStream(Protocol):
    """Открытый async-поток тела: итерация чанками либо чтение целиком."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def read(self) -> bytes: ...


class ResponseStream(ByteStream):
    """ByteStream поверх httpx-ответа; тело не буферизуется до запроса на чтение.

    Индексатор принимает его как AsyncBinaryStream: этот протокол наследовать
    нельзя — транспорт не зависит от boba-indexing.
    """

    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._resp.aiter_bytes()

    async def read(self) -> bytes:
        return await self._resp.aread()


@dataclass(frozen=True)
class HttpResponse:
    "Статус, заголовки и поток тела; stream живёт только внутри блока fetch(...)"

    status: int
    headers: Mapping[str, str]
    stream: ByteStream


class CancellableHttpTransport(HttpTransport):
    """HttpTransport, обрываемый остановкой хода.

    Прерыватель зовут из чужого потока, поэтому он не трогает клиент напрямую,
    а отменяет через loop задачу, которая ведёт запрос: и соединение, и чтение
    тела обрываются на ближайшем await, а не дочитываются до конца.
    """

    def __init__(self, profile: HttpProfile) -> None:
        super().__init__(profile)
        self._cancellation = current_cancellation()
        self._cancellation.raise_if_cancelled()

    @asynccontextmanager
    async def fetch(self, request: HttpRequest) -> AsyncGenerator[HttpResponse, None]:
        self._cancellation.raise_if_cancelled()
        with self._abort_current_task():
            async with super().fetch(request) as resp:
                yield resp

    def _abort_current_task(self) -> AbstractContextManager[None]:
        """Прерыватель на время запроса: отмена хода отменяет эту задачу."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            return nullcontext()

        def cancel_task() -> None:
            loop.call_soon_threadsafe(task.cancel)

        return self._cancellation.abort_with(cancel_task)
