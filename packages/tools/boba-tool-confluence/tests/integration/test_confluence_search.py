"""
ConfluenceSearchTool: unit (MockTransport) + integration (real Confluence)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.agent.workspace_fs import FsProjectWorkspaceRegistry
from boba.config.builder import ConfigBundleFluentFactory
from boba.config.bundle import ConfigBundle, ConfigPath
from boba.config.source.toml import use_toml
from boba.plugin import ExtensionContext
from boba.tool.confluence.plugin import ConfluencePluginConfig
from boba.tool.confluence.search import (
    ConfluenceSearchTool,
    ConfluenceSearchToolConfig,
    SearchArgs,
)
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolSourceId,
)
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceId


@pytest.fixture
def config_fixture() -> ConfigBundle:
    return (
        ConfigBundleFluentFactory()
        .use_cli()
        .use_env_file()
        .use_env()
        .pipe(use_toml)
        .build()
    )


@pytest.fixture
def shell_fixture() -> ProjectWorkspaceShell:
    reg = FsProjectWorkspaceRegistry(Path("local"), "test")
    return reg.get_or_create(WorkspaceId("integration"))


@pytest.fixture
def cfg_fixture(config_fixture: ConfigBundle) -> ConfluenceSearchToolConfig:
    cfg = config_fixture.get(
        ConfluencePluginConfig, ConfigPath.parse("tool.confluence")
    )

    return ConfluenceSearchToolConfig(
        base_url=cfg.base_url,
        auth_method=cfg.auth_method,
        auth_user=cfg.auth_user,
        auth_token=cfg.auth_token,
        timeout_sec=cfg.timeout_sec,
        ssl_verify=cfg.ssl_verify,
        prompt=cfg.confluence_search,
    )


@pytest.fixture
def tool_fixture(
    cfg_fixture: ConfluenceSearchToolConfig, shell_fixture: ProjectWorkspaceShell
) -> ConfluenceSearchTool:
    ctx = ExtensionContext({})

    return ConfluenceSearchTool(cfg_fixture, ctx, ToolSourceId("plugin.confluence"))


@pytest.mark.integration
def test_search_phdd(tool_fixture: ConfluenceSearchTool):
    """Integration: реальный Confluence. данные для подключения из config"""
    # cql = "(text ~ adqm) AND (space=PHDD)"

    result = tool_fixture.execute(
        ToolContext(),
        SearchArgs(
            query="adqm имена таблиц",
            space="PHDD",
            limit=3,
        ),
    )

    assert isinstance(result, JsonResult)
    hits = result.payload["hits"]
    assert isinstance(hits, list)
    for h in hits:
        assert h["page_id"]
