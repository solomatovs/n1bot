"""Pytest fixtures для пакета boba-tool-postgres (integration-mode).

Все тесты — integration: ходят в реальный postgres. Каждая фикстура
`*_cfg` грузит свой tool-конфиг из его TOML-секции (`[tool.pg.<tool>]`).

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.pg.describe_table import DescribeTableConfig
from boba.tool.pg.list_tables import ListTablesConfig
from boba.tool.pg.query import QueryConfig


@pytest.fixture
def query_cfg() -> QueryConfig:
    try:
        return QueryConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.pg.query] не сконфигурирован: {e}")


@pytest.fixture
def list_tables_cfg() -> ListTablesConfig:
    try:
        return ListTablesConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.pg.list_tables] не сконфигурирован: {e}")


@pytest.fixture
def describe_table_cfg() -> DescribeTableConfig:
    try:
        return DescribeTableConfig()
    except ValidationError as e:
        pytest.skip(f"[tool.pg.describe_table] не сконфигурирован: {e}")
