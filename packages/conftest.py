"""Общие фикстуры прогонов: конфиг приложения и anyio-бэкенд."""

from __future__ import annotations

import pytest
from omegaconf import DictConfig

from boba.chainlit.infra.entry import AppEntry
from boba.settings import build_app_config


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Конфиг приложения: BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE."""
    return build_app_config(config_path=AppEntry.config_path())
