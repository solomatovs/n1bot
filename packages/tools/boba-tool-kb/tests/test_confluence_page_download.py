"""Integration `confluence_page_download`: реальная Confluence → HTML в workspace.

Скипается, если не заполнены `[tool.kb.confluence]` (connection) или
`[test.kb].confluence_page_ids` (что качать). Workspace — реальный
`FsProjectWorkspaceShell` на pytest tmp_path; HTTP — реальный
`HttpTransport`; auth — реальный `PatAuth`/`BasicAuth`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.tools.page_download import confluence_page_download
from boba.workspace.contract import ProjectWorkspaceShell

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_page_download_writes_files(
    confluence_cfg: ConfluenceConnectionConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Скачиваем реальные страницы в реальный workspace, проверяем файлы.

    Не проверяем конкретное содержимое (зависит от Confluence оператора),
    только: total == len(page_ids), все page_id в результате, у каждого
    файла есть путь `downloads/{page_id}.html` и непустой `bytes`-поле.
    """
    if not test_cfg.confluence_page_ids:
        pytest.skip(
            "test.kb.confluence_page_ids пусто — задайте список page_id для теста",
        )

    page_ids = test_cfg.confluence_page_ids
    result = confluence_page_download(
        page_ids=page_ids,
        dest_dir="downloads",
        shell=workspace_shell,
        cfg=confluence_cfg,
    )

    assert result["dest_dir"] == "downloads"
    assert result["total"] == len(page_ids)
    saved_ids = {item["page_id"] for item in result["saved"]}
    assert saved_ids == set(page_ids)
    for item in result["saved"]:
        assert item["path"] == f"downloads/{item['page_id']}.html"
        assert int(item["bytes"]) > 0
        # workspace_shell проверяем по факту: файл должен существовать
        assert workspace_shell.exists(item["path"])


def test_confluence_page_download_dest_dir_idempotent(
    confluence_cfg: ConfluenceConnectionConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Повторный вызов в существующий dest_dir не падает; trailing slash чистится."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids[:1]  # одной страницы достаточно
    confluence_page_download(
        page_ids=page_ids, dest_dir="dl", shell=workspace_shell, cfg=confluence_cfg,
    )
    # повторный вызов в существующий dir
    result = confluence_page_download(
        page_ids=page_ids,
        dest_dir="dl/",  # trailing slash
        shell=workspace_shell,
        cfg=confluence_cfg,
    )
    assert result["dest_dir"] == "dl"  # slash отрезан
    assert result["total"] == len(page_ids)


def test_confluence_page_download_as_markdown(
    confluence_cfg: ConfluenceConnectionConfig,
    workspace_shell: ProjectWorkspaceShell,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`as_markdown=True` пишет `.md` с YAML-frontmatter (вместо `.html`)."""
    if not test_cfg.confluence_page_ids:
        pytest.skip("test.kb.confluence_page_ids пусто")

    page_ids = test_cfg.confluence_page_ids
    result = confluence_page_download(
        page_ids=page_ids,
        dest_dir="downloads_md",
        shell=workspace_shell,
        cfg=confluence_cfg,
        as_markdown=True,
    )

    assert result["dest_dir"] == "downloads_md"
    assert result["total"] == len(page_ids)
    for item in result["saved"]:
        assert item["path"] == f"downloads_md/{item['page_id']}.md"
        assert int(item["bytes"]) > 0
        assert workspace_shell.exists(item["path"])
        # YAML-frontmatter: первая строка `---`, есть `page_id: X`.
        with workspace_shell.read_binary(item["path"]) as f:
            head = f.read(400).decode("utf-8", errors="replace")
        assert head.startswith("---\n"), f"missing frontmatter in {item['path']}"
        assert f"page_id: {item['page_id']}" in head
