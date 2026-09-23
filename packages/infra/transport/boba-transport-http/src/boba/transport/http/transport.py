"""HttpTransport: HttpConnection + HttpRequest -> HttpResponse через httpx.AsyncClient.

Единственное место проекта, где создаётся httpx-клиент: адрес, auth и ретраи
берутся из соединения, поведение процесса (таймауты, пул, keepalive,
прокси, дамп) — из HttpTransportConfig. Транспорт только асинхронный:
конвейер индексации ведёт несколько источников сразу, и синхронного
варианта, который занимал бы поток на время сетевого ожидания, здесь нет.

Ошибки:
TransportError — соединение не установлено, тело оборвалось или замолчало
    дольше stream_stall_sec; ошибки httpx наружу не выходят.
HttpStatusError — сервер ответил не-2xx и ретраи исчерпаны; несёт статус,
    фразу ответа и дочитанное тело.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import current_cancellation
from boba.transport.http import HttpxAuth
from boba.transport.http.connection import HttpConnection
from boba.transport.http.dump import DumpingTransport, HttpDumpConfig

T = TypeVar("T")

__all__ = [
    "ByteStream",
    "CancellableHttpTransport",
    "HttpRequest",
    "HttpResponse",
    "HttpStatusError",
    "HttpTransport",
    "HttpTransportConfig",
    "ResponseStream",
    "RetryPolicy",
]

logger = logging.getLogger(__name__)


class TransportError(Exception):
    """Запрос не выполнен: соединение, TLS, обрыв или пауза тела. Текст
    называет метод, адрес и причину httpx."""


class HttpStatusError(TransportError):
    """Сервер ответил не-2xx, и ретраи по политике соединения исчерпаны.
    Тело дочитано и лежит в body: вызывающий кладёт его начало в свой текст."""

    def __init__(self, message: str, *, status: int, reason: str, body: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.body = body


class HttpTransportConfig(BaseModel):
    """Поведение httpx-транспорта процесса: таймауты, пул, keepalive, прокси,
    дамп. Адрес, auth и ретраи живут в соединении (HttpConnection),
    а таймаут чтения — его timeout_sec."""

    model_config = ConfigDict(extra="ignore")

    dump: HttpDumpConfig = Field(
        default_factory=HttpDumpConfig,
        description="Дамп HTTP-обмена в файлы.",
    )

    trust_env: bool = Field(
        default=True,
        description=(
            "Брать прокси и CA из окружения (HTTPS_PROXY, SSL_CERT_FILE); "
            "false — окружение игнорируется целиком."
        ),
    )

    proxy: str = Field(
        default="",
        description="Прокси для запросов; пусто — напрямую.",
    )

    http2: bool = Field(
        default=False,
        description="Разрешить HTTP/2.",
    )

    stream_stall_sec: float = Field(
        default=600,
        ge=0,
        description=(
            "Пауза между кусками тела потокового ответа, секунды; "
            "дольше — TransportError. 0 — без потолка."
        ),
    )

    connect_timeout: float = Field(
        default=5,
        description="Установка TCP-соединения с хостом, включая TLS handshake.",
    )

    write_timeout: float = Field(
        default=100,
        description="Отправка тела запроса на сервер.",
    )

    pool_timeout: float = Field(
        default=5,
        description="Ожидание свободного соединения из пула httpx.",
    )

    max_connections: int = Field(
        default=50,
        description="Потолок одновременных соединений клиента.",
    )

    max_keepalive_connections: int = Field(
        default=10,
        description="Сколько соединений держать открытыми про запас.",
    )

    keepalive_expiry: float = Field(
        default=5,
        description="Сколько секунд простоя живёт неиспользуемое соединение пула.",
    )

    retries: int = Field(
        default=3,
        description="Число повторов установления соединения в httpx-транспорте.",
    )

    tcp_keepalive: bool = Field(
        default=True,
        description=(
            "TCP keepalive (SO_KEEPALIVE): защита от молчаливого разрыва "
            "простаивающего соединения файрволом."
        ),
    )

    tcp_keepidle: int = Field(
        default=60,
        description="TCP_KEEPIDLE: секунд простоя до первой keepalive-пробы.",
    )

    tcp_keepintvl: int = Field(
        default=10,
        description="TCP_KEEPINTVL: интервал между повторными пробами, секунды.",
    )

    tcp_keepcnt: int = Field(
        default=10,
        description=(
            "TCP_KEEPCNT: число безответных проб, после которых соединение "
            "считается мёртвым."
        ),
    )

    tcp_user_timeout: int = Field(
        default=0,
        ge=0,
        description=(
            "TCP_USER_TIMEOUT: сколько миллисекунд ядро ждёт подтверждения "
            "отправленных данных, прежде чем оборвать соединение. 0 — не задавать."
        ),
    )


class RetryPolicy:
    """Политика повторов: 5xx и transport-ошибки, плюс статусы из соединения.

    Сколько попыток положено ошибке, решает она сама: у статуса из
    retry_statuses своё число (throttling просят повторять дольше), у
    остального — общий retry_attempts. Паузу задаёт заголовок Retry-After
    ответа, а без него — линейный backoff соединения.
    """

    RETRY_AFTER: ClassVar[str] = "retry-after"

    def __init__(self, connection: HttpConnection) -> None:
        self._attempts = connection.retry_attempts
        self._backoff = connection.retry_backoff_sec
        self._statuses = connection.retry_statuses
        self._after_cap = connection.retry_after_max_sec

    def attempts_for(self, exc: httpx.HTTPError) -> int:
        """Сколько всего попыток положено этой ошибке; 0 — повторять нельзя."""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if attempts := self._statuses.attempts_for(status):
                return attempts

            if exc.response.is_server_error:
                return self._attempts

            return 0

        if isinstance(exc, httpx.TransportError):
            return self._attempts

        return 0

    def delay(self, attempt: int, exc: httpx.HTTPError) -> float:
        """Пауза перед следующей попыткой: Retry-After сервера или backoff."""
        asked = self._retry_after(exc)
        if asked is None:
            return self._backoff * attempt

        return min(asked, self._after_cap)

    @classmethod
    def _retry_after(cls, exc: httpx.HTTPError) -> float | None:
        """Retry-After ответа в секундах; None — заголовка нет или он не число.

        Спека допускает и HTTP-дату, но её присылают редко: непонятное
        значение уводит запрос на обычный backoff, а не роняет его.
        """
        if not isinstance(exc, httpx.HTTPStatusError):
            return None

        raw = exc.response.headers.get(cls.RETRY_AFTER)
        if raw is None:
            return None

        try:
            seconds = float(raw)
        except ValueError:
            return None

        if seconds < 0:
            return None

        return seconds

    def log(
        self,
        attempt: int,
        limit: int,
        request: HttpRequest,
        exc: httpx.HTTPError,
    ) -> None:
        logger.warning(
            "HTTP %s %s failed (%s: %s); retry %d/%d in %.1fs",
            request.method,
            request.url,
            type(exc).__name__,
            exc,
            attempt,
            limit,
            self.delay(attempt, exc),
        )


class HttpTransport:
    """Исполняет HttpRequest через httpx.AsyncClient, которым владеет.

    Retry покрывает соединение, заголовки и статус; обрыв чтения тела не
    ретраится. Тело отдаётся потоком и живёт, пока открыт блок fetch.
    Файл дампа именуется хостом запроса и меткой DumpLabel из контекста.
    """

    BODY_PREVIEW: ClassVar[int] = 200
    """Сколько символов тела ошибочного ответа входит в текст ошибки."""

    def __init__(self, connection: HttpConnection, config: HttpTransportConfig) -> None:
        self._connection = connection
        self._config = config
        self._retry = RetryPolicy(connection)
        # headers/params на клиент не кладём: они целиком per-request
        self._client = httpx.AsyncClient(
            base_url=connection.root_url(),
            timeout=self._timeout(connection, config),
            transport=self._transport(connection, config),
            auth=HttpxAuth().of(connection),
            trust_env=config.trust_env,
        )

    @staticmethod
    def _timeout(
        connection: HttpConnection, config: HttpTransportConfig
    ) -> httpx.Timeout:
        return httpx.Timeout(
            connect=config.connect_timeout,
            read=connection.timeout_sec,
            write=config.write_timeout,
            pool=config.pool_timeout,
        )

    @classmethod
    def _transport(
        cls, connection: HttpConnection, config: HttpTransportConfig
    ) -> httpx.AsyncHTTPTransport:
        """Транспорт httpx; с включённым дампом обмен пишется в файл."""
        options = cls._transport_options(connection, config)
        if not config.dump.enable:
            return httpx.AsyncHTTPTransport(**options)

        return DumpingTransport(dump_dir=Path(config.dump.path), **options)

    @classmethod
    def _transport_options(
        cls, connection: HttpConnection, config: HttpTransportConfig
    ) -> dict[str, Any]:
        limits = httpx.Limits(
            max_connections=config.max_connections,
            max_keepalive_connections=config.max_keepalive_connections,
            keepalive_expiry=config.keepalive_expiry,
        )
        verify = httpx.create_ssl_context(
            verify=connection.ssl_verify, cert=None, trust_env=config.trust_env
        )

        proxy = None
        if config.proxy:
            proxy = config.proxy

        return {
            "http2": config.http2,
            "verify": verify,
            "limits": limits,
            "proxy": proxy,
            "trust_env": config.trust_env,
            "retries": config.retries,
            "socket_options": cls._socket_options(config),
        }

    @staticmethod
    def _socket_options(config: HttpTransportConfig) -> list[tuple[int, int, int]]:
        """Keepalive против молчаливого разрыва; user timeout — против
        соединения, которое приняло данные и замолчало."""
        options: list[tuple[int, int, int]] = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, int(config.tcp_keepalive)),
        ]

        if config.tcp_keepalive:
            options += [
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, config.tcp_keepidle),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, config.tcp_keepintvl),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, config.tcp_keepcnt),
            ]

        if config.tcp_user_timeout:
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, config.tcp_user_timeout)
            )

        return options

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
                stream=ResponseStream(resp, self._config.stream_stall_sec),
            )
        finally:
            await resp.aclose()

    async def _open_with_retry(self, request: HttpRequest) -> httpx.Response:
        """Соединение + заголовки + статус; сколько повторов — решает политика."""
        attempt = 0
        while True:
            attempt += 1
            resp: httpx.Response | None = None
            try:
                resp = await self._client.send(
                    self._build(request),
                    stream=True,
                    follow_redirects=request.follow_redirects,
                )
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as e:
                if resp is not None:
                    await self._drain(resp)

                limit = self._retry.attempts_for(e)
                if attempt >= limit:
                    raise self._failed(request, e) from e

                self._retry.log(attempt, limit, request, e)
                await asyncio.sleep(self._retry.delay(attempt, e))

    @staticmethod
    async def _drain(resp: httpx.Response) -> None:
        """Тело ответа с ошибкой дочитывается до закрытия: оно уходит в
        HttpStatusError.body."""
        try:
            await resp.aread()
        finally:
            await resp.aclose()

    def _failed(self, request: HttpRequest, exc: httpx.HTTPError) -> TransportError:
        """Ошибка httpx в ошибке слоя: метод, адрес, статус или причина."""
        where = f"{request.method} {self.resolve_url(request)}"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            reason = exc.response.reason_phrase
            body = exc.response.text
            msg = (
                f"{where}: expected 2xx, got {status} {reason}: "
                f"{body[: self.BODY_PREVIEW]!r}"
            )
            return HttpStatusError(msg, status=status, reason=reason, body=body)

        return TransportError(f"{where}: {type(exc).__name__}: {exc}")

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
    follow_redirects: bool = False


class ByteStream(Protocol):
    """Открытый async-поток тела: итерация чанками, строками либо чтение целиком."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    def lines(self) -> AsyncIterator[str]: ...

    async def read(self) -> bytes: ...


class ResponseStream(ByteStream):
    """ByteStream поверх httpx-ответа; тело не буферизуется до запроса на чтение.

    Пауза между кусками тела сверх stall_sec — TransportError: сервер,
    который принял запрос и замолчал посреди потока, не держит вызывающего
    вечно; обрыв тела httpx уходит той же ошибкой. Индексатор принимает
    поток как AsyncBinaryStream: этот протокол наследовать нельзя —
    транспорт не зависит от boba-indexing.
    """

    def __init__(self, resp: httpx.Response, stall_sec: float) -> None:
        self._resp = resp
        self._stall_sec = stall_sec

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._guarded(self._resp.aiter_bytes())

    def lines(self) -> AsyncIterator[str]:
        return self._guarded(self._resp.aiter_lines())

    async def read(self) -> bytes:
        try:
            return await self._resp.aread()
        except httpx.HTTPError as exc:
            raise self._broken(exc) from exc

    async def _guarded(self, pieces: AsyncIterator[T]) -> AsyncIterator[T]:
        """Куски потока под вотчдогом паузы; без потолка — как есть."""
        while True:
            try:
                piece = await self._next(pieces)
            except TimeoutError as exc:
                msg = (
                    f"{self._where()}: response body stalled, no data within "
                    f"stream_stall_sec={self._stall_sec}s"
                )
                raise TransportError(msg) from exc
            except httpx.HTTPError as exc:
                raise self._broken(exc) from exc

            if piece is None:
                return

            yield piece

    def _where(self) -> str:
        return f"{self._resp.request.method} {self._resp.request.url}"

    def _broken(self, exc: httpx.HTTPError) -> TransportError:
        return TransportError(
            f"{self._where()}: reading the response body failed: "
            f"{type(exc).__name__}: {exc}"
        )

    async def _next(self, pieces: AsyncIterator[T]) -> T | None:
        if not self._stall_sec:
            return await anext(pieces, None)

        async with asyncio.timeout(self._stall_sec):
            return await anext(pieces, None)


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

    def __init__(self, connection: HttpConnection, config: HttpTransportConfig) -> None:
        super().__init__(connection, config)
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
