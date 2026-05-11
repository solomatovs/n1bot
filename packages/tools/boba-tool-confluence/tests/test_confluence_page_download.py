"""ConfluencePageDownloadTool: REST-JSON каждой страницы → workspace-файл."""

from __future__ import annotations

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


def _make_tool(shell):
    cfg = ConfluencePageDownloadToolConfig(
        base_url="https://confl.test",
        auth_method="pat",
        auth_user="",
        auth_token="tok",
        timeout_sec=30.0,
        body_format="view",
        prompt=PromptOverlay(),
    )
    ctx = ExtensionContext({ProjectWorkspaceShell: shell})
    return ConfluencePageDownloadTool(cfg, ctx, ToolSourceId("plugin.confluence"))


def _patch_httpx(monkeypatch, handler):
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


def test_downloads_pages_to_workspace_files(monkeypatch):
    pages = {"111": b'{"id":"111","title":"A"}', "222": b'{"id":"222","title":"B"}'}

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
        PageDownloadArgs(page_ids=["111", "222"], dest_dir="downloads"),
    )

    assert isinstance(result, JsonResult)
    payload = result.payload
    assert payload["dest_dir"] == "downloads"
    assert payload["total"] == 2
    assert {item["page_id"] for item in payload["saved"]} == {"111", "222"}
    assert {item["path"] for item in payload["saved"]} == {
        "downloads/111.json",
        "downloads/222.json",
    }
    # Файлы реально записаны с сырым телом REST-ответа
    assert written["downloads/111.json"] == pages["111"]
    assert written["downloads/222.json"] == pages["222"]
    # mkdir вызван один раз, поскольку exists()=False
    shell.mkdir.assert_called_once_with("downloads")


def test_dest_dir_not_created_if_exists(monkeypatch):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    _patch_httpx(monkeypatch, handler)

    shell = MagicMock(spec=ProjectWorkspaceShell)
    shell.exists.return_value = True
    shell.write_binary.side_effect = lambda _path: BytesIO()

    tool = _make_tool(shell)
    tool.execute(
        ToolContext(),
        PageDownloadArgs(page_ids=["1"], dest_dir="existing"),
    )

    shell.mkdir.assert_not_called()


def test_trailing_slash_in_dest_dir_is_stripped(monkeypatch):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    _patch_httpx(monkeypatch, handler)

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
    assert captured == ["dl/7.json"]
    assert result.payload["dest_dir"] == "dl"
