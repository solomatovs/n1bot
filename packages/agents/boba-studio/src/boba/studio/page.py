"""Раздача SPA страницы workflow: любой адрес под {prefix}/workflow — index.html.

Источник задаёт [studio] page: сборка в dist (WorkflowPage, модули из
{prefix}/workflow/assets) либо vite dev-сервер (WorkflowDevPage) — тогда модули
и HMR-сокет vite проксируются под {prefix}/workflow-dev, и бандл собирать не
нужно. В index.html вписываются <base href> и конфиг страницы (префикс, адрес
api, путь socket.io): фронт про префикс не знает.

Ошибки (HTTP):
404 — сборка фронта не выложена в public/workflow.
502 — vite dev-сервер недоступен.
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from starlette.routing import WebSocketRoute
from starlette.staticfiles import StaticFiles
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from boba.runtime.config import StudioConfig

__all__ = [
    "PageAssets",
    "PageStamp",
    "PageUrl",
    "WorkflowDevPage",
    "WorkflowPage",
]

logger = logging.getLogger(__name__)


class PageUrl(StrEnum):
    """Маршруты страницы, её модулей и dev-прокси относительно url_prefix."""

    PAGE = "/workflow/{path:path}"
    ASSETS = "/workflow/assets"
    DEV = "/workflow-dev/{path:path}"

    def under(self, url_prefix: str) -> str:
        return f"{url_prefix}{self.value}"


class PageAssets(StrEnum):
    """Откуда браузер берёт модули страницы относительно url_prefix."""

    BUILT = "/workflow/"
    DEV = "/workflow-dev/"


class PageStamp:
    """Что сервер вписывает в index.html вместо плейсхолдера."""

    PLACEHOLDER: ClassVar[str] = "<!--BOBA_PAGE-->"

    def __init__(self, url_prefix: str, assets: PageAssets, api: StudioConfig) -> None:
        self._prefix = url_prefix
        self._assets = assets
        self._api = api

    def render(self, html: str) -> str:
        config = {
            "prefix": self._prefix,
            "apiPrefix": self._api.api_prefix(),
            "socketPath": self._api.socket_path(),
        }
        stamp = (
            f'<base href="{self._prefix}{self._assets}">'
            f"<script>window.__BOBA_PAGE__ = {json.dumps(config)};</script>"
        )
        return html.replace(self.PLACEHOLDER, stamp, 1)


class WorkflowPage:
    """index.html и модули собранной страницы из каталога dist."""

    INDEX: ClassVar[str] = "index.html"
    ASSETS_DIR: ClassVar[str] = "assets"

    def __init__(self, dist: Path, url_prefix: str, api: StudioConfig) -> None:
        self._dist = dist
        self._prefix = url_prefix
        self._stamp = PageStamp(url_prefix, PageAssets.BUILT, api)

    def mount(self, app: FastAPI) -> None:
        # модули раньше страницы: иначе их перехватит маршрут любого пути
        app.mount(
            PageUrl.ASSETS.under(self._prefix),
            StaticFiles(directory=self._dist / self.ASSETS_DIR, check_dir=False),
            name="workflow-assets",
        )
        app.add_api_route(
            PageUrl.PAGE.under(self._prefix),
            self.serve,
            methods=["GET"],
            include_in_schema=False,
        )

    async def serve(self, path: str = "") -> HTMLResponse:
        index = self._dist / self.INDEX
        if not index.is_file():
            raise HTTPException(status_code=404, detail="workflow page is not built")

        html = self._stamp.render(index.read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class WorkflowDevPage:
    """Прокси vite dev-сервера: index.html со штампом, модули и HMR-сокет."""

    INDEX: ClassVar[str] = "index.html"
    HMR_PROTOCOL: ClassVar[str] = "vite-hmr"

    def __init__(self, dev_url: str, url_prefix: str, api: StudioConfig) -> None:
        self._dev_url = dev_url
        self._prefix = url_prefix
        self._stamp = PageStamp(url_prefix, PageAssets.DEV, api)
        self._client = httpx.AsyncClient(base_url=dev_url, timeout=30.0)

    def mount(self, app: FastAPI) -> None:
        app.router.routes.append(
            WebSocketRoute(PageUrl.DEV.under(self._prefix), self.relay, name="hmr")
        )
        app.add_api_route(
            PageUrl.DEV.under(self._prefix),
            self.proxy,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            PageUrl.PAGE.under(self._prefix),
            self.serve,
            methods=["GET"],
            include_in_schema=False,
        )

    def _upstream_path(self, path: str) -> str:
        return f"{self._prefix}{PageAssets.DEV}{path}"

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
                else:
                    await browser.send_text(frame)
        except WebSocketException:
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
