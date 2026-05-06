"""Fixtures для интеграционных тестов confluence-tools.

Собирают `AppConfig` из реальных source'ов (TOML + env), как делает
production-CLI. Тесты skip'аются при отсутствии base_url/auth_token,
чтобы прогон без настроенного окружения не падал.

Запуск:
    set -a; source local/.env; set +a
    pytest packages/boba-ext-confluence-tools -m integration
"""

from __future__ import annotations

import pytest

from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.ext.confluence_tools.search import ConfluenceSearchSection


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
