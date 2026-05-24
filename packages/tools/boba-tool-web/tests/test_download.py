"""web_download end-to-end: реальный HttpTransport + httpx.MockTransport.

`tmp_workspace` — реальная FS-реализация `ProjectWorkspaceShell`. Mock'аем
только сетевой слой через `httpx.MockTransport`, остальное идёт по
настоящему pipeline'у `WebUrlsRequestSource → HttpTransport → WebArtifact`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from boba.tool.web.auth import NoneAuth
from boba.tool.web.connection import WebConnection
from boba.tool.web.host_profile import WebHostProfile
from boba.tool.web.tools.download import _WebDownloader, web_download
from boba.workspace.contract import ProjectWorkspaceShell


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.Client

    def mock_client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


def _conn() -> WebConnection:
    return WebConnection(
        hosts={"docs.python.org": WebHostProfile(auth=NoneAuth(method="none"))},
    )


def test_download_writes_raw_html_with_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_workspace: ProjectWorkspaceShell,
) -> None:
    body = b"<html><body><h1>Hello</h1></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    _patch_client(monkeypatch, handler)

    dl = _WebDownloader(
        shell=tmp_workspace,
        connection=_conn(),
        dest_dir="downloads",
        as_markdown=False,
    )
    saved = dl.run(["https://docs.python.org/3/library/asyncio.html"])

    assert len(saved) == 1
    record = saved[0]
    assert record["kind"] == "html"
    assert record["url"] == "https://docs.python.org/3/library/asyncio.html"
    assert record["path"] == "downloads/docs.python.org/3/library/asyncio.html"

    with tmp_workspace.read_binary(record["path"]) as f:
        written = f.read()
    assert written.startswith(b"<!--")
    assert b"source: https://docs.python.org/3/library/asyncio.html" in written
    assert written.endswith(body)


def test_download_writes_markdown_converted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_workspace: ProjectWorkspaceShell,
) -> None:
    html = b"<html><body><h1>Title</h1><p>Para</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=html)

    _patch_client(monkeypatch, handler)

    dl = _WebDownloader(
        shell=tmp_workspace,
        connection=_conn(),
        dest_dir="md",
        as_markdown=True,
    )
    saved = dl.run(["https://docs.python.org/x"])
    assert saved[0]["kind"] == "md"
    assert saved[0]["path"].endswith(".md")

    with tmp_workspace.read_text(saved[0]["path"]) as f:
        text = f.read()
    assert text.startswith("---\n")
    assert "source: https://docs.python.org/x" in text
    assert "# Title" in text
    assert "Para" in text


def test_download_blocks_unwhitelisted_host_before_any_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_workspace: ProjectWorkspaceShell,
) -> None:
    called: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        called.append(req)
        return httpx.Response(200, content=b"x")

    _patch_client(monkeypatch, handler)

    dl = _WebDownloader(
        shell=tmp_workspace,
        connection=_conn(),
        dest_dir="d",
        as_markdown=False,
    )
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        dl.run(["https://evil.example.com/x"])
    assert "evil.example.com" in str(exc_info.value)
    assert called == []


def test_web_download_truncates_returned_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_workspace: ProjectWorkspaceShell,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        del req
        return httpx.Response(200, content=b"<html></html>")

    _patch_client(monkeypatch, handler)

    # Прямой вызов tool-функции через её plain callable (минуя framework DI).
    from boba.tool.web.tools.download import WebDownloadConfig

    cfg = WebDownloadConfig.model_construct(
        connection=_conn(),
        dest_dir="bulk",
        max_returned_files=2,
    )
    result = web_download(
        cfg=cfg,
        shell=tmp_workspace,
        urls=[
            "https://docs.python.org/a",
            "https://docs.python.org/b",
            "https://docs.python.org/c",
        ],
        as_markdown=False,
    )
    assert result["total"] == 3
    assert result["returned"] == 2
    assert result["truncated"] is True
    assert len(result["saved"]) == 2


def test_web_download_empty_urls_raises(
    tmp_workspace: ProjectWorkspaceShell,
) -> None:
    from boba.tool.web.tools.download import WebDownloadConfig

    cfg = WebDownloadConfig.model_construct(
        connection=_conn(), dest_dir="d", max_returned_files=None,
    )
    with pytest.raises(ValueError, match="urls пуст") as exc_info:
        web_download(
            cfg=cfg, shell=tmp_workspace, urls=[], as_markdown=False,
        )
    assert "urls" in str(exc_info.value).lower()
