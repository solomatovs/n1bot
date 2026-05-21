"""Integration-test `confluence_page_download_markdown`: реальная Confluence → Markdown.

Симметричен `test_confluence_page_download` (HTML-варианту); разница только
в финальной трансформации: HTML → Markdown через markdownify.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.config import ConfluencePluginConfig
from boba.tool.kb.confluence.page_download_markdown import (
    confluence_page_download_markdown,
)
from boba.workspace.contract import ProjectWorkspaceShell

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_page_download_markdown_writes_files(
    confluence_cfg: ConfluencePluginConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Скачиваем страницы как .md в реальный workspace, проверяем frontmatter."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids
    result = confluence_page_download_markdown(
        page_ids=page_ids,
        dest_dir="downloads_md",
        shell=workspace_shell,
        cfg=confluence_cfg,
    )

    assert result["dest_dir"] == "downloads_md"
    assert result["total"] == len(page_ids)
    saved_ids = {item["page_id"] for item in result["saved"]}
    assert saved_ids == set(page_ids)
    for item in result["saved"]:
        assert item["path"] == f"downloads_md/{item['page_id']}.md"
        assert int(item["bytes"]) > 0
        assert workspace_shell.exists(item["path"])
        # YAML-frontmatter: первая строка `---`, есть `page_id: X`.
        with workspace_shell.read_binary(item["path"]) as f:
            head = f.read(400).decode("utf-8", errors="replace")
        assert head.startswith("---\n"), f"missing frontmatter in {item['path']}"
        assert f"page_id: {item['page_id']}" in head


def test_confluence_page_download_markdown_dest_dir_idempotent(
    confluence_cfg: ConfluencePluginConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Повторный вызов в существующий dest_dir не падает; trailing slash чистится."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids[:1]
    confluence_page_download_markdown(
        page_ids=page_ids,
        dest_dir="dl_md",
        shell=workspace_shell,
        cfg=confluence_cfg,
    )
    result = confluence_page_download_markdown(
        page_ids=page_ids,
        dest_dir="dl_md/",  # trailing slash
        shell=workspace_shell,
        cfg=confluence_cfg,
    )
    assert result["dest_dir"] == "dl_md"
