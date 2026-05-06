"""Fixtures для интеграционных тестов confluence-runtime tools.

Собирают `AppConfig` из реальных source'ов (TOML + env), как делает
production-CLI. Тесты skip'аются при отсутствии base_url/auth_token,
чтобы прогон без настроенного окружения не падал.

Запуск:
    set -a; source local/.env; set +a
    pytest packages/boba-ext-confluence-runtime -m integration
"""

from __future__ import annotations

import pytest

from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.ext.confluence_runtime.tools.page import ConfluencePageSection
from boba.ext.confluence_runtime.tools.page_outline import (
    ConfluencePageOutlineTool,
    ConfluencePageOutlineToolSection,
    PageOutlineArgs,
)
from boba.ext.confluence_runtime.tools.search import (
    ConfluenceSearchSection,
    ConfluenceSearchTool,
    ConfluenceSearchToolSection,
    SearchArgs,
)
from boba.tools.domain import JsonResult, ToolContext


@pytest.fixture(scope="session")
def real_app() -> AppConfig:
    """AppConfig поверх настоящих TOML+env с auto-discovery всех ext-секций.

    Skip-аем session, если `[ext.confluence.search]` не настроена —
    интеграционные тесты бессмысленны без base_url/auth_token.
    """
    boot = AppConfigBootstrap()
    boot.attach_sources(
        [EnvFileSource(), EnvSource(), TomlFileSource(), TomlSource()],
    )
    boot.discover_extension_sections()
    try:
        app = boot.build()
        cfg = app.section(ConfluenceSearchSection)
    except Exception as e:
        pytest.skip(
            f"[ext.confluence.search] не настроена ({type(e).__name__}: {e}); "
            "задайте base_url/auth_token в local/.env или local/config.toml.",
        )
    if not cfg.base_url or not cfg.auth_token:
        pytest.skip("base_url или auth_token пустые — пропускаю integration.")
    return app


@pytest.fixture(scope="session")
def live_page_id(real_app: AppConfig) -> str:
    """Реальный page_id для page-tool'ов.

    Берётся первым результатом из `confluence_search` по широкому термину —
    избавляет от хардкода ID конкретной страницы. Skip всей сессии page-tests,
    если space пустой / поиск ничего не вернул.
    """
    tool = ConfluenceSearchTool(
        tool_cfg=real_app.section(ConfluenceSearchToolSection),
        runtime_cfg=real_app.section(ConfluenceSearchSection),
    )
    ctx = ToolContext(project_workspace=None)  # type: ignore[arg-type]
    result = tool.execute(ctx, SearchArgs(query="page", limit=1))
    assert isinstance(result, JsonResult)
    hits = result.payload["hits"]
    if not hits:
        pytest.skip(
            "search по 'page' вернул 0 hits — нечего проверять для page-tools.",
        )
    return hits[0]["page_id"]


@pytest.fixture(scope="session")
def live_anchor(real_app: AppConfig, live_page_id: str) -> str:
    """Первый anchor с реальной страницы — через outline-tool, как делал бы LLM."""
    tool = ConfluencePageOutlineTool(
        tool_cfg=real_app.section(ConfluencePageOutlineToolSection),
        runtime_cfg=real_app.section(ConfluencePageSection),
    )
    ctx = ToolContext(project_workspace=None)  # type: ignore[arg-type]
    result = tool.execute(
        ctx, PageOutlineArgs(page_id=live_page_id, max_headings=10),
    )
    assert isinstance(result, JsonResult)
    sections = result.payload["sections"]
    if not sections or not sections[0].get("anchor"):
        pytest.skip("у страницы нет заголовков с anchor — нечего читать")
    return sections[0]["anchor"]
