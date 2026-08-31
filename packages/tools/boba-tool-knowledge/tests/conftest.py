"""Фикстуры тестов пакета: сессия приложения не нужна, конфиг — из BOBA_CONFIG_PATH."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from boba.runtime.config import AppLayers


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="session")
def raw_config():
    """Конфиг приложения: проверки идут по нему, а не по выдуманным значениям."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        pytest.skip("BOBA_CONFIG_PATH не задан")

    return AppLayers.compose(Path(config_path))
