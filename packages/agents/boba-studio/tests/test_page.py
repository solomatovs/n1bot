"""Раздача страницы workflow: штамп в index.html, модули из dist, 404 без сборки,
прокси vite dev-сервера — index, модули, HMR-сокет, 502 без сервера."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from boba.runtime.config import BuiltPage, DevPage, StudioConfig
from boba.studio.page import PageStamp, WorkflowDevPage, WorkflowPage

pytestmark = pytest.mark.anyio

PREFIX = "/boba-debug"

INDEX = """<!doctype html>
<html><head><!--BOBA_PAGE--><title>t</title></head>
<body><div id="root"></div></body></html>
"""

DEV_INDEX = """<!doctype html>
<html><head><!--BOBA_PAGE-->
<script type="module" src="/boba-debug/workflow-dev/@vite/client"></script>
</head><body>
<script type="module" src="/boba-debug/workflow-dev/src/main.tsx"></script>
</body></html>
"""


class FakeVite:
    """Подобие vite dev-сервера под {prefix}/workflow-dev: index, модуль, HMR-эхо."""

    MODULE: ClassVar[str] = "export const answer = 42;"

    def __init__(self) -> None:
        self.app = FastAPI()
        self.app.add_api_route(f"{PREFIX}/workflow-dev/index.html", self._index)
        self.app.add_api_route(f"{PREFIX}/workflow-dev/src/main.tsx", self._module)
        self.app.add_api_websocket_route(f"{PREFIX}/workflow-dev/", self._hmr)
        self.port = self._free_port()
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    async def _index(self) -> PlainTextResponse:
        return PlainTextResponse(DEV_INDEX, media_type="text/html")

    async def _module(self, t: str = "") -> PlainTextResponse:
        return PlainTextResponse(
            f"{self.MODULE} // t={t}", media_type="text/javascript"
        )

    async def _hmr(self, websocket: WebSocket) -> None:
        await websocket.accept(subprotocol="vite-hmr")
        await websocket.send_text('{"type":"connected"}')
        text = await websocket.receive_text()
        await websocket.send_text(f"echo:{text}")
        await websocket.close()

    def __enter__(self) -> FakeVite:
        self.thread.start()
        while not self.server.started:
            self.thread.join(0.05)

        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def vite() -> Iterator[FakeVite]:
    with FakeVite() as server:
        yield server


def _api() -> StudioConfig:
    return _studio("built")


def _studio(page: str) -> StudioConfig:
    return StudioConfig.model_validate(
        {
            "host": "127.0.0.1",
            "port": 1,
            "url_prefix": PREFIX,
            "auth_secret": "stand-secret",
            "cookie": "access_token",
            "cookie_samesite": "lax",
            "session_ttl_sec": 3600,
            "page": page,
            "dist": "/nowhere",
        }
    )


def _built_app(dist: Path) -> FastAPI:
    app = FastAPI()
    WorkflowPage(dist, PREFIX, _api()).mount(app)
    return app


def _dev_app(dev_url: str) -> FastAPI:
    app = FastAPI()
    WorkflowDevPage(dev_url, PREFIX, _api()).mount(app)
    return app


async def _get(app: FastAPI, path: str) -> tuple[int, str, str]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://p") as c:
        reply = await c.get(path)

    return reply.status_code, reply.text, reply.headers.get("content-type", "")


async def test_index_is_stamped_for_any_page_path(tmp_path: Path) -> None:
    (tmp_path / WorkflowPage.INDEX).write_text(INDEX)

    status, text, _ = await _get(_built_app(tmp_path), f"{PREFIX}/workflow/run/abc")

    assert status == 200
    assert '<base href="/boba-debug/workflow/">' in text
    assert '"prefix": "/boba-debug"' in text
    assert '"apiPrefix": "/boba-debug/api"' in text
    assert '"socketPath": "/boba-debug/api/socket.io"' in text
    assert PageStamp.PLACEHOLDER not in text


async def test_missing_build_is_reported(tmp_path: Path) -> None:
    status, text, _ = await _get(_built_app(tmp_path / "none"), f"{PREFIX}/workflow/")

    assert status == 404
    assert "not built" in text


async def test_built_modules_are_served_from_assets(tmp_path: Path) -> None:
    (tmp_path / WorkflowPage.INDEX).write_text(INDEX)
    assets = tmp_path / WorkflowPage.ASSETS_DIR
    assets.mkdir()
    (assets / "app-1.js").write_text("export const built = true;")

    status, text, media = await _get(
        _built_app(tmp_path), f"{PREFIX}/workflow/assets/app-1.js"
    )

    assert status == 200
    assert text == "export const built = true;"
    assert "javascript" in media


def test_page_config_parses_built_and_dev_sources() -> None:
    built = _studio("built")
    dev = _studio("http://127.0.0.1:5173/")

    assert isinstance(built.page, BuiltPage)
    assert isinstance(dev.page, DevPage)
    assert dev.page.url == "http://127.0.0.1:5173"

    with pytest.raises(ValueError, match="page"):
        _studio("somewhere")


async def test_dev_index_is_stamped_with_dev_assets(vite: FakeVite) -> None:
    status, text, _ = await _get(_dev_app(vite.url), f"{PREFIX}/workflow/observe/abc")

    assert status == 200
    assert '<base href="/boba-debug/workflow-dev/">' in text
    assert 'src="/boba-debug/workflow-dev/src/main.tsx"' in text
    assert '"socketPath": "/boba-debug/api/socket.io"' in text
    assert PageStamp.PLACEHOLDER not in text


async def test_dev_modules_are_proxied_with_query(vite: FakeVite) -> None:
    status, text, media = await _get(
        _dev_app(vite.url), f"{PREFIX}/workflow-dev/src/main.tsx?t=7"
    )

    assert status == 200
    assert text == f"{FakeVite.MODULE} // t=7"
    assert "javascript" in media


async def test_dev_server_down_is_502(vite: FakeVite) -> None:
    down = f"http://127.0.0.1:{FakeVite._free_port()}"

    status, text, _ = await _get(_dev_app(down), f"{PREFIX}/workflow/")

    assert status == 502
    assert "not reachable" in text


def test_hmr_socket_is_relayed_both_ways(vite: FakeVite) -> None:
    client = TestClient(_dev_app(vite.url))
    hmr = client.websocket_connect(
        f"{PREFIX}/workflow-dev/?token=x", subprotocols=["vite-hmr"]
    )
    with hmr as ws:
        assert ws.accepted_subprotocol == "vite-hmr"
        assert ws.receive_text() == '{"type":"connected"}'
        ws.send_text('{"type":"ping"}')
        assert ws.receive_text() == 'echo:{"type":"ping"}'
