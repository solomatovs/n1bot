"""Фикстуры тестов пакета: сессия приложения не нужна, конфиг — из BOBA_CONFIG_PATH."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass
