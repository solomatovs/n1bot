"""Раздача страницы workflow: штамп префикса в index.html, 404 без сборки,
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

from boba.chainlit.domain.config import BuiltPage, DevPage
from boba.chainlit.workflow.page import (
    PageStamp,
    WorkflowDevPage,
    WorkflowPage,
    WorkflowPageConfig,
)
from boba.runtime.config import ApiConfig

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


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Страница не зависит от сессии chainlit."""


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


def _api() -> ApiConfig:
    return ApiConfig(
        host="127.0.0.1",
        port=1,
        url_prefix=PREFIX,
        auth_secret="stand-secret",
        cookie="access_token",
    )


def _built_app(app_root: Path) -> FastAPI:
    app = FastAPI()
    WorkflowPage(str(app_root), PREFIX, _api()).mount(app)
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
    public = tmp_path / WorkflowPage.PUBLIC_DIR
    public.mkdir(parents=True)
    (public / WorkflowPage.INDEX).write_text(INDEX)

    status, text, _ = await _get(_built_app(tmp_path), "/workflow/run/abc")

    assert status == 200
    assert '<base href="/boba-debug/public/workflow/">' in text
    assert '"prefix": "/boba-debug"' in text
    assert '"apiPrefix": "/boba-debug/api"' in text
    assert '"socketPath": "/boba-debug/api/socket.io"' in text
    assert PageStamp.PLACEHOLDER not in text


async def test_missing_build_is_reported(tmp_path: Path) -> None:
    status, text, _ = await _get(_built_app(tmp_path), "/workflow/")

    assert status == 404
    assert "not built" in text


def test_page_config_parses_built_and_dev_sources() -> None:
    built = WorkflowPageConfig.model_validate({"page": "built"})
    dev = WorkflowPageConfig.model_validate({"page": "http://127.0.0.1:5173/"})

    assert isinstance(built.page, BuiltPage)
    assert isinstance(dev.page, DevPage)
    assert dev.page.url == "http://127.0.0.1:5173"

    with pytest.raises(ValueError, match="page"):
        WorkflowPageConfig.model_validate({"page": "somewhere"})


async def test_dev_index_is_stamped_with_dev_assets(vite: FakeVite) -> None:
    status, text, _ = await _get(_dev_app(vite.url), "/workflow/observe/abc")

    assert status == 200
    assert '<base href="/boba-debug/workflow-dev/">' in text
    assert 'src="/boba-debug/workflow-dev/src/main.tsx"' in text
    assert '"socketPath": "/boba-debug/api/socket.io"' in text
    assert PageStamp.PLACEHOLDER not in text


async def test_dev_modules_are_proxied_with_query(vite: FakeVite) -> None:
    status, text, media = await _get(
        _dev_app(vite.url), "/workflow-dev/src/main.tsx?t=7"
    )

    assert status == 200
    assert text == f"{FakeVite.MODULE} // t=7"
    assert media.startswith("text/javascript")


async def test_dev_server_down_is_502(vite: FakeVite) -> None:
    down = f"http://127.0.0.1:{FakeVite._free_port()}"

    status, text, _ = await _get(_dev_app(down), "/workflow/")

    assert status == 502
    assert "not reachable" in text


def test_hmr_socket_is_relayed_both_ways(vite: FakeVite) -> None:
    client = TestClient(_dev_app(vite.url))
    hmr = client.websocket_connect("/workflow-dev/?token=x", subprotocols=["vite-hmr"])
    with hmr as ws:
        assert ws.accepted_subprotocol == "vite-hmr"
        assert ws.receive_text() == '{"type":"connected"}'
        ws.send_text('{"type":"ping"}')
        assert ws.receive_text() == 'echo:{"type":"ping"}'
