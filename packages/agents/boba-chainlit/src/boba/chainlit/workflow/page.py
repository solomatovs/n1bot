"""Раздача SPA страницы workflow: любой адрес под /workflow — index.html.

Источник задаёт [workflow] page: сборка в public/workflow (WorkflowPage)
либо vite dev-сервер (WorkflowDevPage) — тогда модули и HMR-сокет vite
проксируются под /workflow-dev, и бандл собирать не нужно. В index.html
вписываются <base href> под url_prefix приложения и конфиг страницы
(префикс, путь socket.io): фронт про префикс не знает.

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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from boba.api.app import ApiApp
from boba.chainlit.domain.config import BuiltPage, DevPage, PageSource

__all__ = [
    "PageAssets",
    "PageStamp",
    "PageUrl",
    "WorkflowDevPage",
    "WorkflowPage",
    "WorkflowPageConfig",
]

logger = logging.getLogger(__name__)


class WorkflowPageConfig(BaseModel):
    """Секция [workflow]: откуда отдаётся страница."""

    model_config = ConfigDict(extra="ignore")

    page: BuiltPage | DevPage = Field(
        discriminator="kind",
        description="'built' — сборка из public/workflow; адрес — vite dev-сервер.",
    )

    @field_validator("page", mode="before")
    @classmethod
    def _parse_page(cls, raw: object) -> object:
        return PageSource.parse(raw)


class PageUrl(StrEnum):
    """Маршруты страницы и её dev-прокси у хоста."""

    PAGE = "/workflow/{path:path}"
    DEV = "/workflow-dev/{path:path}"


class PageAssets(StrEnum):
    """Откуда браузер берёт модули страницы относительно url_prefix."""

    BUILT = "/public/workflow/"
    DEV = "/workflow-dev/"


class PageStamp:
    """Что сервер вписывает в index.html вместо плейсхолдера."""

    PLACEHOLDER: ClassVar[str] = "<!--BOBA_PAGE-->"

    def __init__(self, url_prefix: str, assets: PageAssets) -> None:
        self._prefix = url_prefix
        self._assets = assets

    def render(self, html: str) -> str:
        config = {
            "prefix": self._prefix,
            "apiPrefix": ApiApp.mount_prefix(self._prefix),
            "socketPath": ApiApp.socket_path(self._prefix),
        }
        stamp = (
            f'<base href="{self._prefix}{self._assets}">'
            f"<script>window.__BOBA_PAGE__ = {json.dumps(config)};</script>"
        )
        return html.replace(self.PLACEHOLDER, stamp, 1)


class WorkflowPage:
    """index.html собранной страницы; статику отдаёт chainlit из /public."""

    INDEX: ClassVar[str] = "index.html"
    PUBLIC_DIR: ClassVar[str] = "public/workflow"

    def __init__(self, app_root: str, url_prefix: str) -> None:
        self._index = Path(app_root) / self.PUBLIC_DIR / self.INDEX
        self._stamp = PageStamp(url_prefix, PageAssets.BUILT)

    def mount(self, app: FastAPI) -> None:
        app.add_api_route(
            str(PageUrl.PAGE), self.serve, methods=["GET"], include_in_schema=False
        )
        app.router.routes.insert(0, app.router.routes.pop())

    async def serve(self, path: str = "") -> HTMLResponse:
        if not self._index.is_file():
            raise HTTPException(status_code=404, detail="workflow page is not built")

        html = self._stamp.render(self._index.read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class WorkflowDevPage:
    """Прокси vite dev-сервера: index.html со штампом, модули и HMR-сокет."""

    INDEX: ClassVar[str] = "index.html"
    HMR_PROTOCOL: ClassVar[str] = "vite-hmr"

    def __init__(self, dev_url: str, url_prefix: str) -> None:
        self._dev_url = dev_url
        self._prefix = url_prefix
        self._stamp = PageStamp(url_prefix, PageAssets.DEV)
        self._client = httpx.AsyncClient(base_url=dev_url, timeout=30.0)

    def mount(self, app: FastAPI) -> None:
        app.router.routes.insert(
            0, WebSocketRoute(str(PageUrl.DEV), self.relay, name="workflow-hmr")
        )
        app.add_api_route(
            str(PageUrl.DEV), self.proxy, methods=["GET"], include_in_schema=False
        )
        app.router.routes.insert(0, app.router.routes.pop())
        app.add_api_route(
            str(PageUrl.PAGE), self.serve, methods=["GET"], include_in_schema=False
        )
        app.router.routes.insert(0, app.router.routes.pop())

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
