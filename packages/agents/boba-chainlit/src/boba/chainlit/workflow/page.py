"""Раздача SPA страницы workflow: любой адрес под /workflow — index.html сборки.

В index.html вписываются <base href> под url_prefix приложения и конфиг
страницы (префикс, путь socket.io): сборка фронта про префикс не знает.

Ошибки (HTTP):
404 — сборка фронта не выложена в public/workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from boba.chainlit.domain.keys import WorkflowUrl

__all__ = ["PageStamp", "WorkflowPage"]


class PageStamp:
    """Что сервер вписывает в index.html вместо плейсхолдера."""

    PLACEHOLDER: ClassVar[str] = "<!--BOBA_PAGE-->"
    PUBLIC_PATH: ClassVar[str] = "/public/workflow/"
    SOCKET_PATH: ClassVar[str] = "/ws/socket.io"

    def __init__(self, url_prefix: str) -> None:
        self._prefix = url_prefix

    def render(self, html: str) -> str:
        config = {
            "prefix": self._prefix,
            "socketPath": f"{self._prefix}{self.SOCKET_PATH}",
        }
        stamp = (
            f'<base href="{self._prefix}{self.PUBLIC_PATH}">'
            f"<script>window.__BOBA_PAGE__ = {json.dumps(config)};</script>"
        )
        return html.replace(self.PLACEHOLDER, stamp, 1)


class WorkflowPage:
    """index.html собранной страницы; статику отдаёт chainlit из /public."""

    INDEX: ClassVar[str] = "index.html"
    PUBLIC_DIR: ClassVar[str] = "public/workflow"

    def __init__(self, app_root: str, url_prefix: str) -> None:
        self._index = Path(app_root) / self.PUBLIC_DIR / self.INDEX
        self._stamp = PageStamp(url_prefix)

    def mount(self, app: FastAPI) -> None:
        app.add_api_route(
            str(WorkflowUrl.PAGE), self.serve, methods=["GET"], include_in_schema=False
        )
        app.router.routes.insert(0, app.router.routes.pop())

    async def serve(self, path: str = "") -> HTMLResponse:
        if not self._index.is_file():
            raise HTTPException(status_code=404, detail="workflow page is not built")

        html = self._stamp.render(self._index.read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
