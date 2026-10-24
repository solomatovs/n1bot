"""TCP-прокси стенда с переключаемым отказом: между чатом и службой агента
или сервером MCP. Тест включает отказ, который бывает в сети: соединение не
принимается, обрывается, зависает или вместо сервиса отвечает чужой HTTP
(401, 403, 502, страница HTML).

Ошибки:
FaultProxyError — прокси не поднялся или не переключился в срок.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar

from boba.stand.ui.stand import StandPorts, StandUrl

__all__ = ["FaultMode", "FaultProxy", "FaultProxyError", "FaultReply"]

logger = logging.getLogger(__name__)


class FaultProxyError(RuntimeError):
    """Прокси стенда не поднялся или не переключился в срок."""


class FaultMode(StrEnum):
    """Поведение прокси для новых и открытых соединений."""

    THROUGH = "through"
    """Байты идут к сервису как есть."""

    REFUSE = "refuse"
    """Порт не слушается: соединение отклоняется, открытые обрываются."""

    DROP = "drop"
    """Открытые соединения обрываются, дальше — как REFUSE: сервис пропал."""

    HANG = "hang"
    """Соединение принимается, ответа нет."""

    REPLY = "reply"
    """На каждый запрос HTTP — заданный ответ (FaultReply), сервис не
    участвует."""


@dataclass(frozen=True)
class FaultReply:
    """Ответ HTTP вместо сервиса: статус, вид тела и тело."""

    status: HTTPStatus
    content_type: str
    body: str

    HTML_PAGE: ClassVar[str] = (
        "<!doctype html><html><head><title>Sign in</title></head><body>"
        "<h1>Corporate gateway</h1><p>Please sign in to continue.</p></body></html>"
    )

    def wire(self) -> bytes:
        body = self.body.encode()
        head = (
            f"HTTP/1.1 {self.status.value} {self.status.phrase}\r\n"
            f"Content-Type: {self.content_type}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        )
        return head.encode() + body


class FaultProxy:
    """Прокси на свободном порту в своём потоке и цикле событий: тест зовёт
    его синхронно из любого потока. upstream — порт сервиса за прокси.
    Создают фикстуры ui- и look-тестов сбоев."""

    HOST: ClassVar[str] = StandUrl.HOST.value
    CHUNK: ClassVar[int] = 65536
    WAIT_SEC: ClassVar[float] = 10.0
    HANG_SEC: ClassVar[float] = 3600.0
    HEAD_END: ClassVar[bytes] = b"\r\n\r\n"
    LENGTH: ClassVar[bytes] = b"content-length:"

    def __init__(self, upstream: int) -> None:
        self.port = StandPorts().free()
        self._upstream = upstream
        self._mode = FaultMode.THROUGH
        self._reply: FaultReply | None = None
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"fault-proxy-{self.port}", daemon=True
        )
        self._thread.start()
        self._run(self._listen)

    def set(self, mode: FaultMode, reply: FaultReply | None = None) -> None:
        """Режим mode для новых соединений; DROP и REFUSE обрывают открытые,
        HANG и REPLY — тоже, чтобы ни одно не шло мимо отказа.

        Ошибки:
        FaultProxyError — REPLY без ответа или прокси не переключился.
        """
        if mode is FaultMode.REPLY and reply is None:
            msg = "fault proxy: mode reply needs the HTTP reply to give"
            raise FaultProxyError(msg)

        self._mode = mode
        self._reply = reply
        self._run(self._applied)

    def stop(self) -> None:
        self._run(self._closed)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(self.WAIT_SEC)

    def _run(self, step: Callable[[], Coroutine[Any, Any, None]]) -> None:
        future = asyncio.run_coroutine_threadsafe(step(), self._loop)
        try:
            future.result(self.WAIT_SEC)
        except TimeoutError as exc:
            msg = f"fault proxy on port {self.port}: no switch within {self.WAIT_SEC}s"
            raise FaultProxyError(msg) from exc

    async def _applied(self) -> None:
        mode = self._mode
        if mode is FaultMode.REFUSE or mode is FaultMode.DROP:
            await self._unlisten()
            self._drop_all()
            return

        await self._listen()
        if mode is not FaultMode.THROUGH:
            self._drop_all()

    async def _closed(self) -> None:
        await self._unlisten()
        self._drop_all()

    async def _listen(self) -> None:
        if self._server is not None:
            return

        self._server = await asyncio.start_server(self._serve, self.HOST, self.port)

    async def _unlisten(self) -> None:
        server = self._server
        if server is None:
            return

        self._server = None
        server.close()

    def _drop_all(self) -> None:
        for writer in list(self._writers):
            writer.transport.abort()

        self._writers.clear()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            await self._served(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
            logger.debug("fault proxy on port %s: connection ended: %s", self.port, exc)
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _served(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._mode is FaultMode.HANG:
            await asyncio.sleep(self.HANG_SEC)
            return

        reply = self._reply
        if self._mode is FaultMode.REPLY and reply is not None:
            await self._answered(reader, writer, reply)
            return

        await self._piped(reader, writer)

    async def _answered(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        reply: FaultReply,
    ) -> None:
        """Каждый запрос соединения получает reply; тело запроса читается
        по Content-Length."""
        while True:
            head = await reader.readuntil(self.HEAD_END)
            length = self._length(head)
            if length:
                await reader.readexactly(length)

            writer.write(reply.wire())
            await writer.drain()

    def _length(self, head: bytes) -> int:
        for line in head.split(b"\r\n"):
            if line.lower().startswith(self.LENGTH):
                return int(line.split(b":", 1)[1])

        return 0

    async def _piped(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            self.HOST, self._upstream
        )
        self._writers.add(upstream_writer)
        try:
            await asyncio.gather(
                self._copied(reader, upstream_writer),
                self._copied(upstream_reader, writer),
            )
        finally:
            self._writers.discard(upstream_writer)
            upstream_writer.close()

    async def _copied(
        self, source: asyncio.StreamReader, target: asyncio.StreamWriter
    ) -> None:
        try:
            while chunk := await source.read(self.CHUNK):
                target.write(chunk)
                await target.drain()
        finally:
            target.close()
