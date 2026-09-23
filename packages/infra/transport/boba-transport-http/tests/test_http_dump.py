"""Дамп HTTP-обмена HttpTransport: байты запроса и ответа ложатся в файл по хосту."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from boba.transport.http import (
    HttpDumpConfig,
    HttpRequest,
    HttpTransport,
    HttpTransportConfig,
)
from boba.transport.http.connection import HttpConnection, UrlScheme

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _Handler(BaseHTTPRequestHandler):
    BODY = b'{"who":"dump-test"}'

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()


class TestHttpDump:
    async def test_exchange_lands_in_host_file(
        self, server: ThreadingHTTPServer, tmp_path: Path
    ) -> None:
        host, port = server.server_address[:2]
        connection = HttpConnection(
            scheme=UrlScheme.HTTP, host=str(host), port=int(port)
        )
        dump = HttpDumpConfig(enable=True, path=str(tmp_path / "dumps"))
        config = HttpTransportConfig(dump=dump)

        async with (
            HttpTransport(connection, config) as transport,
            transport.fetch(HttpRequest(url="/rest/api/space/DEV")) as resp,
        ):
            body = await resp.stream.read()

        assert body == _Handler.BODY

        dumped = (tmp_path / "dumps" / f"{host}.log").read_text(encoding="utf-8")
        assert "GET /rest/api/space/DEV HTTP/1.1" in dumped
        assert '{"who":"dump-test"}' in dumped

    async def test_disabled_dump_writes_nothing(
        self, server: ThreadingHTTPServer, tmp_path: Path
    ) -> None:
        host, port = server.server_address[:2]
        connection = HttpConnection(
            scheme=UrlScheme.HTTP, host=str(host), port=int(port)
        )

        async with (
            HttpTransport(connection, HttpTransportConfig()) as transport,
            transport.fetch(HttpRequest(url="/")) as resp,
        ):
            await resp.stream.read()

        assert not list(tmp_path.iterdir())
