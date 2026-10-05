"""Конфиг приложения обязан разбираться моделями.

Проверяется тот же файл, с которым работает приложение: путь берётся из
BOBA_CONFIG_PATH, как и в остальных тестах. Правки структуры (новая секция,
обязательное поле, лимит профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import pytest
from omegaconf import DictConfig

from boba.chainlit.infra.config import AppConfig
from boba.config import bind


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestConfigStaysValid:
    """Конфиг разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=AppConfig)
