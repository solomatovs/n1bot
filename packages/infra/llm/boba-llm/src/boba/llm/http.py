"""HTTP-слой LLM-провайдеров: сборка httpx-клиента и обмен чата с ретраями.

Ошибки:
ChatProviderError — endpoint недоступен, ответил статусом или пауза потока
    превысила потолок конфига; выпускает ChatExchange.
Ошибки httpx из LlmHttp уходят вызывающему как есть.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar

import httpx

from boba.chat.http import HttpConfig
from boba.chat.provider import ChatProviderError
from boba.llm.dump import DumpingTransport

logger = logging.getLogger(__name__)

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

__all__ = ["ChatExchange", "LlmHttp", "WireStream"]


class LlmHttp:
    """Сборка httpx-клиента и таймаутов из HttpConfig — одна на всех потребителей."""

    @staticmethod
    def timeout(c: HttpConfig) -> httpx.Timeout:
        return httpx.Timeout(
            connect=c.connect_timeout,
            read=c.read_timeout,
            write=c.write_timeout,
            pool=c.pool_timeout,
        )

    @classmethod
    def client(
        cls,
        c: HttpConfig,
        dump_file: Callable[[httpx.Request], str] | None = None,
    ) -> httpx.AsyncClient:
        """Клиент с транспортом по конфигу; при dump.enable обмен пишется в файлы."""
        if c.dump.enable:
            transport: httpx.AsyncHTTPTransport = cls._dump_transport(c, dump_file)
        else:
            transport = httpx.AsyncHTTPTransport(**cls._transport_options(c))

        return httpx.AsyncClient(
            timeout=cls.timeout(c),
            transport=transport,
        )

    @classmethod
    def _dump_transport(
        cls,
        c: HttpConfig,
        dump_file: Callable[[httpx.Request], str] | None,
    ) -> DumpingTransport:
        label = dump_file
        if label is None:
            label = cls._host_dump_file

        return DumpingTransport(
            dump_dir=Path(c.dump.path),
            dump_file=label,
            **cls._transport_options(c),
        )

    @staticmethod
    def _host_dump_file(request: httpx.Request) -> str:
        return f"{request.url.host}.log"

    @classmethod
    def _transport_options(cls, c: HttpConfig) -> dict[str, Any]:
        limits = httpx.Limits(
            max_connections=c.max_connections,
            max_keepalive_connections=c.max_keepalive_connections,
            keepalive_expiry=c.keepalive_expiry,
        )
        verify = httpx.create_ssl_context(
            verify=c.ssl_verify, cert=None, trust_env=c.trust_env
        )

        proxy = None
        if c.proxy:
            proxy = c.proxy

        return {
            "http2": c.http2,
            "verify": verify,
            "limits": limits,
            "proxy": proxy,
            "trust_env": c.trust_env,
            "retries": c.retries,
            "socket_options": cls._socket_options(c),
        }

    @staticmethod
    def _socket_options(c: HttpConfig) -> list[tuple[int, int, int]]:
        """Опции сокета: keepalive против молчаливого разрыва, user timeout —
        против соединения, которое приняло данные и замолчало."""
        options: list[tuple[int, int, int]] = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, int(c.tcp_keepalive)),
        ]

        if c.tcp_keepalive:
            options += [
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, c.tcp_keepidle),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, c.tcp_keepintvl),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, c.tcp_keepcnt),
            ]

        if c.tcp_user_timeout:
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, c.tcp_user_timeout)
            )

        return options


class WireStream(Protocol[T_co]):
    """Разбор строк потока в чанки wire-формата; экземпляр живёт одну попытку."""

    @abstractmethod
    def feed(self, line: str) -> T_co | None: ...

    @abstractmethod
    def finish(self) -> T_co | None:
        """Недосланный чанк оборвавшегося потока; None — отдавать нечего."""
        ...


class ChatExchange:
    """HTTP-обмен чат-провайдера: ретраи по статусам, повтор до первого чанка
    и вотчдог пауз между строками потока.

    Wire-формат обмену безразличен: тело собирает провайдер, строки потока
    разбирает его WireStream. Один экземпляр обслуживает один endpoint.
    """

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        cfg: HttpConfig,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        api_key: str,
        label: str,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._endpoint = endpoint
        self._api_key = api_key
        self._label = label

    @property
    def _where(self) -> str:
        """Метка провайдера и адрес запроса для текстов ошибок."""
        return f"{self._label}: POST {self._endpoint}"

    async def complete(self, payload: dict[str, Any]) -> bytes:
        """Один запрос-ответ: тело ответа целиком, разбор — у провайдера."""
        attempts = self._cfg.max_retries + 1

        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    self._endpoint, json=payload, headers=self._headers()
                )
                if response.status_code in self.RETRY_STATUSES:
                    raise httpx.TransportError(f"status {response.status_code}")

                if response.is_error:
                    msg = (
                        f"{self._where} expected 2xx, got {response.status_code}: "
                        f"{response.content[:500]!r}"
                    )
                    raise ChatProviderError(msg)

                return response.content
            except ChatProviderError:
                raise
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    msg = (
                        f"{self._where} failed after {attempts} attempt(s): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise ChatProviderError(msg) from exc

                logger.warning(
                    "%s: attempt %d/%d failed: %s: %s",
                    self._where,
                    attempt + 1,
                    attempts,
                    type(exc).__name__,
                    exc,
                )

        msg = (
            f"{self._where}: no attempts were made, "
            f"max_retries={self._cfg.max_retries} gives {attempts} attempt(s)"
        )
        raise ChatProviderError(msg)

    async def stream(
        self,
        payload: dict[str, Any],
        decoder: Callable[[], WireStream[T]],
    ) -> AsyncIterator[T]:
        """Чанки потока; до первого разобранного чанка запрос повторяется."""
        attempts = self._cfg.max_retries + 1

        for attempt in range(attempts):
            streamed = False
            try:
                async for chunk in self._attempt(payload, decoder()):
                    streamed = True
                    yield chunk

                return
            except ChatProviderError:
                raise
            except httpx.HTTPError as exc:
                if streamed:
                    msg = (
                        f"{self._where}: stream broke mid-reply: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise ChatProviderError(msg) from exc

                if attempt + 1 >= attempts:
                    msg = (
                        f"{self._where} failed after {attempts} attempt(s): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise ChatProviderError(msg) from exc

                logger.warning(
                    "%s: attempt %d/%d failed: %s: %s",
                    self._where,
                    attempt + 1,
                    attempts,
                    type(exc).__name__,
                    exc,
                )

    async def _attempt(
        self,
        payload: dict[str, Any],
        decoder: WireStream[T],
    ) -> AsyncIterator[T]:
        async with self._client.stream(
            "POST", self._endpoint, json=payload, headers=self._headers()
        ) as response:
            if response.status_code in self.RETRY_STATUSES:
                # тело не нужно: статус ретраится как сетевая ошибка
                raise httpx.TransportError(f"status {response.status_code}")

            if response.is_error:
                body = await response.aread()
                msg = (
                    f"{self._where} expected 2xx, got {response.status_code}: "
                    f"{body[:500]!r}"
                )
                raise ChatProviderError(msg)

            lines = response.aiter_lines()
            while True:
                line = await self._next_line(lines)
                if line is None:
                    break

                chunk = decoder.feed(line)
                if chunk is None:
                    continue

                yield chunk

            trailing = decoder.finish()
            if trailing is None:
                return

            yield trailing

    async def _next_line(self, lines: AsyncIterator[str]) -> str | None:
        """Очередная строка потока под вотчдогом паузы между строками."""
        ceiling = self._cfg.stream_chunk_timeout

        try:
            if ceiling:
                async with asyncio.timeout(ceiling):
                    return await anext(lines, None)
            return await anext(lines, None)
        except TimeoutError as exc:
            msg = (
                f"{self._where}: stream stalled, no line within "
                f"stream_chunk_timeout={ceiling}s"
            )
            raise ChatProviderError(msg) from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}
