"""Pytest fixtures для пакета boba-tool-postgres (integration-mode).

Все тесты — integration: ходят в реальный postgres. Фикстуры грузят
SqlExecutorConfig из секции плагина [tool.pg].

pytest -m integration для запуска; default-режим (-m "not integration")
их исключает (см. root pyproject.toml).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.settings import bind, build_app_config
from boba.tool.pg.executor import SqlExecutorConfig


def _pg_cfg() -> SqlExecutorConfig:
    try:
        # получаем настройки всего приложения
        if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
            raise ValueError("please pass env BOBA_CONFIG_PATH")

        return bind(
            build_app_config(config_path=Path(config_path), argv=sys.argv[1:]),
            "tool.pg",
            SqlExecutorConfig,
        )
        return bind(build_app_config(), "tool.pg", SqlExecutorConfig)
    except ValidationError as e:
        pytest.skip(f"[tool.pg] не сконфигурирован: {e}")


@pytest.fixture
def query_cfg() -> SqlExecutorConfig:
    return _pg_cfg()


@pytest.fixture
def list_tables_cfg() -> SqlExecutorConfig:
    return _pg_cfg()


@pytest.fixture
def describe_table_cfg() -> SqlExecutorConfig:
    return _pg_cfg()
