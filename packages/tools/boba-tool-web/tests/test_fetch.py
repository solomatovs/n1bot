"""web_fetch end-to-end: streaming line-window прямо из response, без FS."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from boba.tool.web.auth import NoneAuth
from boba.tool.web.connection import WebConnection
from boba.tool.web.host_profile import WebHostProfile
from boba.tool.web.tools.fetch import WebFetchConfig, web_fetch


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.Client

    def mock_client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


def _cfg() -> WebFetchConfig:
    return WebFetchConfig.model_construct(
        connection=WebConnection(
            hosts={
                "docs.python.org": WebHostProfile(
                    hostname="docs.python.org",
                    auth=NoneAuth(method="none"),
                ),
            },
        ),
    )


def test_fetch_returns_dict_with_first_window_of_raw_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"line0\nline1\nline2\nline3\nline4\n"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=body)

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=False,
        line_offset=0,
        line_count=3,
    )
    assert out == {
        "content": "line0\nline1\nline2",
        "path": "https://docs.python.org/x",
        "total_lines": 5,
        "returned_lines": 3,
    }


def test_fetch_window_skips_into_body(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"line0\nline1\nline2\nline3\nline4\n"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=body)

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=False,
        line_offset=2,
        line_count=2,
    )
    assert out["content"] == "line2\nline3"
    assert out["returned_lines"] == 2
    assert out["total_lines"] == 5


def test_fetch_window_past_eof_returns_empty_with_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"only-one\n"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=body)

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=False,
        line_offset=100,
        line_count=10,
    )
    assert out["content"] == ""
    assert out["returned_lines"] == 0
    # `body` без trailing-line после \n даёт 1 строку
    assert out["total_lines"] == 1


def test_fetch_html_no_trailing_newline_counts_last_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Хвост без \\n — это всё равно одна строка."""

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=b"a\nb\nc")

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=False,
        line_offset=0,
        line_count=100,
    )
    assert out["content"] == "a\nb\nc"
    assert out["total_lines"] == 3


def test_fetch_html_streams_across_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тело больше CHUNK_SIZE и newlines падают на середину чанков."""
    body = b"\n".join(f"line-{i:05d}".encode() for i in range(2000)) + b"\n"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=body)

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=False,
        line_offset=1995,
        line_count=3,
    )
    assert out["content"] == "line-01995\nline-01996\nline-01997"
    assert out["total_lines"] == 2000
    assert out["returned_lines"] == 3


def test_fetch_markdown_returns_markdown_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"<html><body><h1>Title</h1><p>Para</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=html)

    _patch_client(monkeypatch, handler)

    out = web_fetch(
        cfg=_cfg(),
        url="https://docs.python.org/x",
        as_markdown=True,
        line_offset=0,
        line_count=50,
    )
    # markdownify даёт `# Title\n\nPara` и тп — нет frontmatter'а, чистый MD
    assert "# Title" in out["content"]
    assert "Para" in out["content"]
    assert out["path"] == "https://docs.python.org/x"
    assert out["total_lines"] >= 1


def test_fetch_blocks_unwhitelisted_host_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        called.append(req)
        return httpx.Response(200, content=b"x")

    _patch_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        web_fetch(
            cfg=_cfg(),
            url="https://evil.example.com/x",
            as_markdown=False,
            line_offset=0,
            line_count=5,
        )
    assert "evil.example.com" in str(exc_info.value)
    assert called == []
