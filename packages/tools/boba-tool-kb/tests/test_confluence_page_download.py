"""Integration `confluence_page_download`: реальная Confluence → HTML в workspace."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.tools.page_download import (
    ConfluencePageDownloadConfig,
    confluence_page_download,
)
from boba.workspace.contract import ProjectWorkspaceShell

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_page_download_writes_files(
    confluence_page_download_cfg: ConfluencePageDownloadConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Скачиваем реальные страницы в реальный workspace, проверяем файлы."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids
    result = confluence_page_download(
        cfg=confluence_page_download_cfg,
        shell=workspace_shell,
        page_ids=page_ids,
        dest_dir="downloads",
    )

    assert result["dest_dir"] == "downloads"
    assert result["total"] == len(page_ids)
    saved_ids = {item["page_id"] for item in result["saved"]}
    assert saved_ids == set(page_ids)
    for item in result["saved"]:
        assert item["path"] == f"downloads/{item['page_id']}.html"
        assert int(item["bytes"]) > 0
        assert workspace_shell.exists(item["path"])


def test_confluence_page_download_dest_dir_idempotent(
    confluence_page_download_cfg: ConfluencePageDownloadConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Повторный вызов в существующий dest_dir не падает; trailing slash чистится."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids[:1]
    confluence_page_download(
        cfg=confluence_page_download_cfg,
        shell=workspace_shell,
        page_ids=page_ids,
        dest_dir="dl",
    )
    result = confluence_page_download(
        cfg=confluence_page_download_cfg,
        shell=workspace_shell,
        page_ids=page_ids,
        dest_dir="dl/",
    )
    assert result["dest_dir"] == "dl"
    assert result["total"] == len(page_ids)


def test_confluence_page_download_as_markdown(
    confluence_page_download_cfg: ConfluencePageDownloadConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`as_markdown=True` пишет `.md` с YAML-frontmatter."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids
    result = confluence_page_download(
        cfg=confluence_page_download_cfg,
        shell=workspace_shell,
        page_ids=page_ids,
        dest_dir="downloads_md",
        as_markdown=True,
    )

    assert result["dest_dir"] == "downloads_md"
    assert result["total"] == len(page_ids)
    for item in result["saved"]:
        assert item["path"] == f"downloads_md/{item['page_id']}.md"
        assert int(item["bytes"]) > 0
        assert workspace_shell.exists(item["path"])
        with workspace_shell.read_binary(item["path"]) as f:
            head = f.read(400).decode("utf-8", errors="replace")
        assert head.startswith("---\n"), f"missing frontmatter in {item['path']}"
        assert f"page_id: {item['page_id']}" in head
