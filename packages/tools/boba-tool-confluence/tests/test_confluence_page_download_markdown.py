"""ConfluencePageDownloadMarkdownTool: HTML → Markdown → workspace-файл."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import httpx

from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tool.confluence.page_download_markdown import (
    ConfluencePageDownloadMarkdownTool,
    ConfluencePageDownloadMarkdownToolConfig,
    PageDownloadMarkdownArgs,
)
from boba.tools.domain import JsonResult, ToolContext, ToolSourceId
from boba.workspace.contract import ProjectWorkspaceShell


def _make_tool(shell):
    cfg = ConfluencePageDownloadMarkdownToolConfig(
        base_url="https://confl.test",
        auth_method="pat",
        auth_user="",
        auth_token="tok",
        timeout_sec=30.0,
        body_format="view",
        prompt=PromptOverlay(),
    )
    ctx = ExtensionContext({ProjectWorkspaceShell: shell})
    return ConfluencePageDownloadMarkdownTool(
        cfg, ctx, ToolSourceId("plugin.confluence"),
    )


def _patch_httpx(monkeypatch, handler):
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


def test_downloads_pages_as_markdown(monkeypatch):
    pages = {
        "111": (
            b'{"id":"111","title":"Alpha",'
            b'"body":{"view":{"value":"<h1>Heading</h1><p>Body <b>bold</b></p>"}}}'
        ),
        "222": (
            b'{"id":"222","title":"Beta",'
            b'"body":{"view":{"value":"<h2>Sub</h2><p>Just text</p>"}}}'
        ),
    }

    def handler(req: httpx.Request) -> httpx.Response:
        for pid, body in pages.items():
            if f"/content/{pid}" in req.url.path:
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)

    written: dict[str, bytes] = {}

    def _write_binary(path: str):
        buf = BytesIO()
        original_close = buf.close

        def _close():
            written[path] = buf.getvalue()
            original_close()

        buf.close = _close  # type: ignore[method-assign]
        return buf

    shell = MagicMock(spec=ProjectWorkspaceShell)
    shell.exists.return_value = False
    shell.write_binary.side_effect = _write_binary

    tool = _make_tool(shell)
    result = tool.execute(
        ToolContext(),
        PageDownloadMarkdownArgs(
            page_ids=["111", "222"],
            dest_dir="downloads",
        ),
    )

    assert isinstance(result, JsonResult)
    payload = result.payload
    assert payload["dest_dir"] == "downloads"
    assert payload["total"] == 2
    assert {item["page_id"] for item in payload["saved"]} == {"111", "222"}
    assert {item["path"] for item in payload["saved"]} == {
        "downloads/111.md",
        "downloads/222.md",
    }
    assert {item["title"] for item in payload["saved"]} == {"Alpha", "Beta"}

    md_111 = written["downloads/111.md"].decode("utf-8")
    md_222 = written["downloads/222.md"].decode("utf-8")
    # markdownify рендерит h1 как `# Heading`, **bold** для <b>.
    assert "# Heading" in md_111
    assert "**bold**" in md_111
    assert "## Sub" in md_222
    assert "Just text" in md_222
    shell.mkdir.assert_called_once_with("downloads")


def test_trailing_slash_stripped(monkeypatch):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"id":"7","body":{"view":{"value":"<p>x</p>"}}}',
        )

    _patch_httpx(monkeypatch, handler)

    shell = MagicMock(spec=ProjectWorkspaceShell)
    shell.exists.return_value = True
    captured: list[str] = []
    shell.write_binary.side_effect = lambda path: (captured.append(path), BytesIO())[1]

    tool = _make_tool(shell)
    result = tool.execute(
        ToolContext(),
        PageDownloadMarkdownArgs(page_ids=["7"], dest_dir="dl/"),
    )

    assert isinstance(result, JsonResult)
    assert captured == ["dl/7.md"]
    assert result.payload["dest_dir"] == "dl"
