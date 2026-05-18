"""ConfluencePageDownloadTool: HTML каждой страницы → workspace-файл."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from unittest.mock import MagicMock

import httpx

from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tool.confluence.page_download import (
    ConfluencePageDownloadTool,
    ConfluencePageDownloadToolConfig,
    PageDownloadArgs,
)
from boba.tools.domain import JsonResult, ToolContext, ToolSourceId
from boba.workspace.contract import ProjectWorkspaceShell

_HTTPX_TARGET = "boba.transport.http.transport.httpx.Client"


def _make_tool(shell):
    cfg = ConfluencePageDownloadToolConfig(
        base_url="https://confl.test",
        auth_method="pat",
        auth_user="",
        auth_token="tok",
        timeout_sec=30.0,
        body_format="view",
        ssl_verify=False,
        prompt=PromptOverlay(),
    )
    ctx = ExtensionContext({ProjectWorkspaceShell: shell})
    return ConfluencePageDownloadTool(cfg, ctx, ToolSourceId("plugin_confluence"))


def test_downloads_pages_to_workspace_files(
    patch_httpx: Callable[[str, Callable[[httpx.Request], httpx.Response]], None],
):
    pages = {
        "111": (
            b'{"id":"111","title":"A","space":{"key":"DOC"},'
            b'"body":{"view":{"value":"<p>page A</p>"}}}'
        ),
        "222": (
            b'{"id":"222","title":"B","space":{"key":"DOC"},'
            b'"body":{"view":{"value":"<p>page B</p>"}}}'
        ),
    }
    html = {
        "111": b"<p>page A</p>",
        "222": b"<p>page B</p>",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        for pid, body in pages.items():
            if f"/content/{pid}" in req.url.path:
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    patch_httpx(_HTTPX_TARGET, handler)

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
        PageDownloadArgs(page_ids=["111", "222"], dest_dir="downloads"),
    )

    assert isinstance(result, JsonResult)
    payload = result.payload
    assert payload["dest_dir"] == "downloads"
    assert payload["total"] == 2
    assert {item["page_id"] for item in payload["saved"]} == {"111", "222"}
    assert {item["path"] for item in payload["saved"]} == {
        "downloads/111.html",
        "downloads/222.html",
    }
    assert {item["title"] for item in payload["saved"]} == {"A", "B"}
    assert {item["space_key"] for item in payload["saved"]} == {"DOC"}
    urls = {item["url"] for item in payload["saved"]}
    assert "https://confl.test/pages/viewpage.action?pageId=111" in urls
    assert "https://confl.test/pages/viewpage.action?pageId=222" in urls
    # Файлы начинаются с HTML-комментария с метаданными и заканчиваются телом
    for pid in ("111", "222"):
        content = written[f"downloads/{pid}.html"]
        assert content.startswith(b"<!--")
        assert f"page_id: {pid}".encode() in content
        assert b"space: DOC" in content
        assert content.endswith(html[pid])
    # mkdir вызван один раз, поскольку exists()=False
    shell.mkdir.assert_called_once_with("downloads")


def test_dest_dir_not_created_if_exists(
    patch_httpx: Callable[[str, Callable[[httpx.Request], httpx.Response]], None],
):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    patch_httpx(_HTTPX_TARGET, handler)

    shell = MagicMock(spec=ProjectWorkspaceShell)
    shell.exists.return_value = True
    shell.write_binary.side_effect = lambda _path: BytesIO()

    tool = _make_tool(shell)
    tool.execute(
        ToolContext(),
        PageDownloadArgs(page_ids=["1"], dest_dir="existing"),
    )

    shell.mkdir.assert_not_called()


def test_trailing_slash_in_dest_dir_is_stripped(
    patch_httpx: Callable[[str, Callable[[httpx.Request], httpx.Response]], None],
):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    patch_httpx(_HTTPX_TARGET, handler)

    shell = MagicMock(spec=ProjectWorkspaceShell)
    shell.exists.return_value = True
    captured: list[str] = []
    shell.write_binary.side_effect = lambda path: (captured.append(path), BytesIO())[1]

    tool = _make_tool(shell)
    result = tool.execute(
        ToolContext(),
        PageDownloadArgs(page_ids=["7"], dest_dir="dl/"),
    )

    assert isinstance(result, JsonResult)
    assert captured == ["dl/7.html"]
    assert result.payload["dest_dir"] == "dl"
