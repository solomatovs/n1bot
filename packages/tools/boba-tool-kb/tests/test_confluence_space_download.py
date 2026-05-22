"""Integration `confluence_space_download`: все страницы Confluence space → workspace.

Скипается, если не заполнены `[tool.kb.confluence]` (connection) или
`[test.kb].confluence_space_key`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.tools.space_download import confluence_space_download
from boba.workspace.contract import ProjectWorkspaceShell

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_space_download_writes_files(
    confluence_cfg: ConfluenceConnectionConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Скачиваем все страницы реального space-а в workspace, проверяем файлы."""
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    result = confluence_space_download(
        space_key=test_cfg.confluence_space_key,
        dest_dir="space_dl",
        shell=workspace_shell,
        cfg=confluence_cfg,
    )

    assert result["dest_dir"] == "space_dl"
    assert result["space_key"] == test_cfg.confluence_space_key
    assert result["total"] >= 1, (
        f"space {test_cfg.confluence_space_key!r} вернул 0 страниц"
    )
    for item in result["saved"]:
        assert item["path"] == f"space_dl/{item['page_id']}.html"
        assert int(item["bytes"]) > 0
        assert workspace_shell.exists(item["path"])


def test_confluence_space_download_as_markdown(
    confluence_cfg: ConfluenceConnectionConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`as_markdown=True` для space — пишет `.md` с frontmatter'ом."""
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    result = confluence_space_download(
        space_key=test_cfg.confluence_space_key,
        dest_dir="space_dl_md",
        shell=workspace_shell,
        cfg=confluence_cfg,
        as_markdown=True,
    )

    assert result["total"] >= 1
    for item in result["saved"]:
        assert item["path"] == f"space_dl_md/{item['page_id']}.md"
        assert workspace_shell.exists(item["path"])
