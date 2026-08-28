"""Фронт стенда: один адрес для браузера, за ним chainlit и studio по префиксу пути.

Как nginx в проде: {prefix}/api и {prefix}/workflow* уходят в studio, остальное —
в chainlit; HTTP идёт потоком, WebSocket ретранслируется кадр в кадр.

Ошибки:
StandError — фронт не поднялся в отведённое время.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import ClassVar

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

__all__ = ["FrontDoor", "FrontRoutes"]


class FrontRoutes:
    """Порт upstream по пути запроса: studio под /api и /workflow*, иначе chainlit."""

    STUDIO_HEADS: ClassVar[tuple[str, ...]] = ("/api", "/workflow", "/workflow-dev")

    def __init__(self, url_prefix: str, chainlit_port: int, studio_port: int) -> None:
        self._prefix = url_prefix
        self._chainlit = chainlit_port
        self._studio = studio_port

    def port_of(self, path: str) -> int:
        rest = path.removeprefix(self._prefix)
        for head in self.STUDIO_HEADS:
            if rest == head:
                return self._studio

            if rest.startswith(f"{head}/"):
                return self._studio

        return self._chainlit


logger = logging.getLogger(__name__)


class FrontDoor:
    """Обратный прокси стенда на своём порту в фоновом потоке; слушает все адреса,
    как приложение: SSO-тест ходит на домен стенда, а не на loopback."""

    HOP_BY_HOP: ClassVar[frozenset[str]] = frozenset(
        {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }
    )
    WS_FORWARDED: ClassVar[frozenset[str]] = frozenset({"cookie", "authorization"})
    METHODS: ClassVar[list[str]] = [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    ]
    READY_SEC: ClassVar[float] = 30.0

    def __init__(self, port: int, routes: FrontRoutes) -> None:
        self._port = port
        self._routes = routes
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
        app = Starlette(
            routes=[
                WebSocketRoute("/{path:path}", self.relay),
                Route("/{path:path}", self.proxy, methods=self.METHODS),
            ]
        )
        config = uvicorn.Config(
            app,
            host="0.0.0.0",  # noqa: S104
            port=port,
            log_level="warning",
            ws="wsproto",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + self.READY_SEC
        while time.monotonic() < deadline:
            if self._server.started:
                return

            time.sleep(0.05)

        msg = f"stand front did not start on {self._port} in {self.READY_SEC}s"
        raise RuntimeError(msg)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    def _upstream(self, scheme: str, path: str, query: str) -> str:
        url = f"{scheme}://127.0.0.1:{self._routes.port_of(path)}{path}"
        if query:
            url = f"{url}?{query}"

        return url

    def _forwarded(self, host: str) -> list[tuple[str, str]]:
        return [("x-forwarded-host", host), ("x-forwarded-proto", "http")]

    async def proxy(self, request: Request) -> Response:
        headers: list[tuple[str, str]] = []
        for name, value in request.headers.items():
            if name.lower() in self.HOP_BY_HOP:
                continue

            headers.append((name, value))

        headers.extend(self._forwarded(request.headers.get("host", "")))
        url = self._upstream("http", request.url.path, request.url.query)
        # запрос собирается мимо build_request: иначе клиент подмешал бы cookie из
        # своей банки, и вход одного браузера утёк бы во все следующие запросы
        upstream_request = httpx.Request(
            request.method, url, headers=headers, content=request.stream()
        )
        upstream = await self._client.send(upstream_request, stream=True)

        raw: list[tuple[bytes, bytes]] = []
        for name, value in upstream.headers.raw:
            if name.decode().lower() in self.HOP_BY_HOP:
                continue

            raw.append((name, value))

        response = StreamingResponse(
            self._body(upstream), status_code=upstream.status_code
        )
        response.raw_headers = raw

        return response

    @staticmethod
    async def _body(upstream: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    async def relay(self, websocket: WebSocket) -> None:
        headers: list[tuple[str, str]] = []
        for name, value in websocket.headers.items():
            if name.lower() in self.WS_FORWARDED:
                headers.append((name, value))

        headers.extend(self._forwarded(websocket.headers.get("host", "")))
        offered = list(websocket.scope.get("subprotocols", []))
        url = self._upstream("ws", websocket.url.path, websocket.url.query)

        try:
            upstream = await connect(
                url,
                subprotocols=offered or None,  # type: ignore[arg-type]
                additional_headers=headers,
                origin=websocket.headers.get("origin"),  # type: ignore[arg-type]
                max_size=None,
            )
        except (OSError, WebSocketException) as exc:
            await websocket.close(code=1011, reason=f"upstream is not reachable: {exc}")
            return

        await websocket.accept(subprotocol=upstream.subprotocol)
        try:
            await self._pump(websocket, upstream)
        finally:
            await upstream.close()

    async def _pump(self, browser: WebSocket, upstream: ClientConnection) -> None:
        downstream_task = asyncio.create_task(self._to_browser(browser, upstream))
        upstream_task = asyncio.create_task(self._to_upstream(browser, upstream))
        done, pending = await asyncio.wait(
            {downstream_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        for task in done:
            ended = (
                "browser->upstream" if task is upstream_task else "upstream->browser"
            )
            failure = task.exception()
            logger.debug("ws relay ended on %s: %s", ended, failure)
            task.result()

    @staticmethod
    async def _to_browser(browser: WebSocket, upstream: ClientConnection) -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await browser.send_bytes(frame)
                else:
                    await browser.send_text(frame)
        except (WebSocketException, WebSocketDisconnect, RuntimeError):
            return

    @staticmethod
    async def _to_upstream(browser: WebSocket, upstream: ClientConnection) -> None:
        try:
            while True:
                event = await browser.receive()
                if event["type"] == "websocket.disconnect":
                    return

                text = event.get("text")
                if text is not None:
                    await upstream.send(text)
                    continue

                data = event.get("bytes")
                if data is not None:
                    await upstream.send(data)
        except (WebSocketDisconnect, WebSocketException):
            return
