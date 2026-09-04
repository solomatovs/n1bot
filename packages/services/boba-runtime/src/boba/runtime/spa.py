"""Раздача одностраничного приложения под префиксом: любой адрес страницы отдаёт
index.html, модули идут со сборки в dist либо проксируются с vite dev-сервера.

В index.html вписываются <base href> и конфиг страницы: фронт про префикс
приложения не знает. Страница (workflow в studio, каталог в chainlit) задаёт
только свои пути SpaPaths и словарь конфига для окна.

Ошибки (HTTP):
404 — сборка фронта не выложена в dist.
502 — vite dev-сервер недоступен.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.routing import WebSocketRoute
from starlette.staticfiles import StaticFiles
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

__all__ = [
    "BuiltSpa",
    "DevSpa",
    "SpaPaths",
    "SpaStamp",
]

logger = logging.getLogger(__name__)


class SpaPaths(BaseModel):
    """Пути страницы относительно url_prefix: маршрут любого адреса страницы,
    точка монтирования модулей сборки, маршрут dev-прокси и базы модулей.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    page: str = Field(pattern=r"^/.+\{path:path\}$")
    assets: str = Field(pattern=r"^/.+")
    dev: str = Field(pattern=r"^/.+\{path:path\}$")
    built_base: str = Field(pattern=r"^/.+/$")
    dev_base: str = Field(pattern=r"^/.+/$")

    @staticmethod
    def under(url_prefix: str, path: str) -> str:
        return f"{url_prefix}{path}"


class SpaStamp:
    """Что сервер вписывает в index.html вместо плейсхолдера: base и конфиг окна."""

    PLACEHOLDER: ClassVar[str] = "<!--BOBA_PAGE-->"
    WINDOW_KEY: ClassVar[str] = "__BOBA_PAGE__"

    def __init__(
        self, url_prefix: str, assets_base: str, config: Mapping[str, str]
    ) -> None:
        self._prefix = url_prefix
        self._assets_base = assets_base
        self._config = dict(config)

    def render(self, html: str) -> str:
        stamp = (
            f'<base href="{self._prefix}{self._assets_base}">'
            f"<script>window.{self.WINDOW_KEY} = {json.dumps(self._config)};</script>"
        )
        return html.replace(self.PLACEHOLDER, stamp, 1)


class BuiltSpa:
    """index.html и модули собранной страницы из каталога dist."""

    INDEX: ClassVar[str] = "index.html"
    ASSETS_DIR: ClassVar[str] = "assets"

    def __init__(
        self,
        paths: SpaPaths,
        dist: Path,
        url_prefix: str,
        config: Mapping[str, str],
    ) -> None:
        self._paths = paths
        self._dist = dist
        self._prefix = url_prefix
        self._stamp = SpaStamp(url_prefix, paths.built_base, config)

    def mount(self, app: FastAPI) -> None:
        # модули раньше страницы: иначе их перехватит маршрут любого пути
        app.mount(
            self._paths.under(self._prefix, self._paths.assets),
            StaticFiles(directory=self._dist / self.ASSETS_DIR, check_dir=False),
            name=self._paths.name,
        )
        app.add_api_route(
            self._paths.under(self._prefix, self._paths.page),
            self.serve,
            methods=["GET"],
            include_in_schema=False,
        )

    async def serve(self, path: str = "") -> HTMLResponse:
        index = self._dist / self.INDEX
        if not index.is_file():
            raise HTTPException(status_code=404, detail="page is not built")

        html = self._stamp.render(index.read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class DevSpa:
    """Прокси vite dev-сервера: index.html со штампом, модули и HMR-сокет."""

    INDEX: ClassVar[str] = "index.html"

    def __init__(
        self,
        paths: SpaPaths,
        dev_url: str,
        url_prefix: str,
        config: Mapping[str, str],
    ) -> None:
        self._paths = paths
        self._dev_url = dev_url
        self._prefix = url_prefix
        self._stamp = SpaStamp(url_prefix, paths.dev_base, config)
        self._client = httpx.AsyncClient(base_url=dev_url, timeout=30.0)

    def mount(self, app: FastAPI) -> None:
        dev = self._paths.under(self._prefix, self._paths.dev)
        app.router.routes.append(
            WebSocketRoute(dev, self.relay, name=f"{self._paths.name}-hmr")
        )
        app.add_api_route(dev, self.proxy, methods=["GET"], include_in_schema=False)
        app.add_api_route(
            self._paths.under(self._prefix, self._paths.page),
            self.serve,
            methods=["GET"],
            include_in_schema=False,
        )

    def _upstream_path(self, path: str) -> str:
        return f"{self._prefix}{self._paths.dev_base}{path}"

    async def _fetch(self, path: str, query: str) -> httpx.Response:
        url = self._upstream_path(path)
        if query:
            url = f"{url}?{query}"

        try:
            return await self._client.get(url)
        except httpx.HTTPError as exc:
            detail = f"vite dev server is not reachable at {self._dev_url}: {exc}"
            raise HTTPException(status_code=502, detail=detail) from exc

    async def serve(self, path: str = "") -> HTMLResponse:
        upstream = await self._fetch(self.INDEX, "")
        if upstream.status_code != httpx.codes.OK:
            detail = f"vite dev server answered {upstream.status_code} for index.html"
            raise HTTPException(status_code=502, detail=detail)

        html = self._stamp.render(upstream.text)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def proxy(self, request: Request, path: str) -> Response:
        upstream = await self._fetch(path, request.url.query)
        media_type = upstream.headers.get("content-type", "application/octet-stream")

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    def _ws_url(self, path: str, query: str) -> str:
        base = self._dev_url.replace("http", "ws", 1)
        url = f"{base}{self._upstream_path(path)}"
        if query:
            url = f"{url}?{query}"

        return url

    async def relay(self, websocket: WebSocket) -> None:
        """Сокет HMR: кадры vite ходят в обе стороны как есть."""
        path = str(websocket.path_params.get("path", ""))
        offered = list(websocket.scope.get("subprotocols", []))

        try:
            upstream = await connect(
                self._ws_url(path, websocket.url.query), subprotocols=offered
            )
        except (OSError, WebSocketException) as exc:
            logger.warning(
                "vite hmr socket is not reachable at %s: %s", self._dev_url, exc
            )
            await websocket.close(code=1011, reason="vite dev server is not reachable")
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
            task.result()

    @staticmethod
    async def _to_browser(browser: WebSocket, upstream: ClientConnection) -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await browser.send_bytes(frame)
                    continue

                await browser.send_text(frame)
        except WebSocketException:
            return

    @staticmethod
    async def _to_upstream(browser: WebSocket, upstream: ClientConnection) -> None:
        try:
            while True:
                message = await browser.receive()
                if message["type"] == "websocket.disconnect":
                    return

                text = message.get("text")
                if text is not None:
                    await upstream.send(text)
                    continue

                payload = message.get("bytes")
                if payload is not None:
                    await upstream.send(payload)
        except WebSocketDisconnect:
            return
