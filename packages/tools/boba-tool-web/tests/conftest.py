"""Фикстуры пакета: локальный http-сервер и изоляция от chainlit-контекста."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest
from web_sandbox import PageHandler


@pytest.fixture(scope="session")
def http_origin() -> Iterator[str]:
    """Адрес поднятого сервера вида http://127.0.0.1:PORT."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address[:2]

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass
