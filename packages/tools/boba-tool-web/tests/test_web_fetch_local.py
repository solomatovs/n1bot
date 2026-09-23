"""web_fetch_page по локальному серверу: html как markdown и как есть, текст,
картинка без OCR, окно строк, чужой хост и отказ сервера."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from PIL import Image

from boba.doc.config import DisabledOcrConfig
from boba.tool.web.tools import WebToolsConfig, web_fetch_page, web_grep_page
from boba.toolkit.entry import ToolMain
from boba.transport.http import HttpStatusError
from boba.transport.http.connection import HttpConnection, UnknownHostError, UrlScheme

pytestmark = [pytest.mark.anyio]

HTML = (
    b"<html><head><title>t</title><script>var x;</script></head>"
    b"<body><h1>Stand page</h1><p>first <b>bold</b></p><p>second</p></body></html>"
)
TEXT = b"line one\nline two\nline three\n"


class _Quiet(SimpleHTTPRequestHandler):
    """Статика без журнала запросов в stderr."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _Site:
    """Каталог статики под http.server в фоновом потоке."""

    def __init__(self, root: Path) -> None:
        self._root = root
        handler = partial(_Quiet, directory=str(root))
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _Site:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


@pytest.fixture
def site(tmp_path: Path) -> Iterator[_Site]:
    (tmp_path / "page.html").write_bytes(HTML)
    (tmp_path / "notes.txt").write_bytes(TEXT)
    picture = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(picture, format="PNG")
    (tmp_path / "logo.png").write_bytes(picture.getvalue())

    with _Site(tmp_path) as running:
        yield running


@pytest.fixture
def connection(site: _Site) -> HttpConnection:
    return HttpConnection(scheme=UrlScheme.HTTP, host="127.0.0.1", port=site.port)


@pytest.fixture
def cfg() -> WebToolsConfig:
    return WebToolsConfig(
        spool_memory_limit=1 << 20,
        text_encodings=("utf-8",),
        ocr=DisabledOcrConfig(provider="off"),
    )


def _fetch():
    body = ToolMain.toolset(web_fetch_page)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    return body


async def test_html_as_markdown(site: _Site, connection, cfg) -> None:
    result = await _fetch()(
        url=site.url("page.html"),
        connection=connection,
        as_markdown=True,
        line_offset=0,
        line_count=50,
        cfg=cfg,
    )

    assert result.language == "markdown"
    assert result.text.startswith("# Stand page")
    assert "first **bold**" in result.text
    assert "var x" not in result.text
    assert result.metadata["kind"] == "html"


async def test_html_as_is(site: _Site, connection, cfg) -> None:
    result = await _fetch()(
        url=site.url("page.html"),
        connection=connection,
        as_markdown=False,
        line_offset=0,
        line_count=50,
        cfg=cfg,
    )

    assert result.language == "html"
    assert result.text == HTML.decode()


async def test_text_line_window(site: _Site, connection, cfg) -> None:
    result = await _fetch()(
        url=site.url("notes.txt"),
        connection=connection,
        as_markdown=True,
        line_offset=1,
        line_count=1,
        cfg=cfg,
    )

    assert result.language == "text"
    assert result.text == "line two"
    assert result.note == f"url={site.url('notes.txt')}; lines 2-2 of 3"


async def test_image_without_ocr_is_empty(site: _Site, connection, cfg) -> None:
    result = await _fetch()(
        url=site.url("logo.png"),
        connection=connection,
        as_markdown=True,
        line_offset=0,
        line_count=5,
        cfg=cfg,
    )

    assert result.text == ""
    assert result.metadata["kind"] == "image"


async def test_grep_over_markdown(site: _Site, connection, cfg) -> None:
    body = ToolMain.toolset(web_grep_page)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(
        url=site.url("page.html"),
        connection=connection,
        pattern="second",
        cfg=cfg,
    )

    assert "second" in result.text
    assert result.note is not None
    assert "matches: 1" in result.note


async def test_missing_page_is_a_status_error(site: _Site, connection, cfg) -> None:
    with pytest.raises(HttpStatusError) as caught:
        await _fetch()(
            url=site.url("missing.html"),
            connection=connection,
            as_markdown=True,
            line_offset=0,
            line_count=5,
            cfg=cfg,
        )

    assert caught.value.status == 404
    assert "expected 2xx, got 404" in str(caught.value)


async def test_foreign_host_is_refused(site: _Site, connection, cfg) -> None:
    with pytest.raises(UnknownHostError):
        await _fetch()(
            url="http://example.com/",
            connection=connection,
            as_markdown=True,
            line_offset=0,
            line_count=5,
            cfg=cfg,
        )
