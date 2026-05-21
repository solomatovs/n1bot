"""Pytest fixtures для пакета boba-tool-postgres.

Все живые тесты — operator-mode: ходят в реальный postgres + реальный
embeddings endpoint. Включаются через `[tool.postgres]` в config.toml
(`dsn`, `embedding_*`, `ingest_folder`, `ingest_collection`) или
соответствующими `BOBA_TOOL__POSTGRES__*` env'ами. Без конфигурации —
тесты skip-ятся.

Маркер `integration` — для интеграционных тестов на внешние сервисы;
по умолчанию в репо исключены (см. root `pyproject.toml`). Запуск:
    pytest packages/tools/boba-tool-postgres -m integration -s
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.indexing.context import CollectionId
from boba.tool.postgres.config import PostgresPluginConfig


@dataclass(frozen=True)
class OperatorRunSpec:
    """Параметры одного operator-mode прогона ingest.

    `cfg` — реальный `PostgresPluginConfig`, собранный из env'а и
    `$BOBA_CONFIG_PATH`. Из cfg берутся dsn и embedding_* параметры.

    `folder` и `collection` — per-run параметры, дублируются здесь для
    удобства (источник — cfg).
    """

    cfg: PostgresPluginConfig
    folder: Path
    collection: CollectionId


@pytest.fixture
def operator_run() -> OperatorRunSpec | None:
    """Operator-mode прогон. None если `cfg.ingest_folder` пуст или dsn не задан.

    Параметры читаются через `PostgresPluginConfig` — единая система
    конфигурирования (TOML `[tool.postgres]` + env `BOBA_TOOL__POSTGRES__*`).
    """
    try:
        cfg = PostgresPluginConfig()
    except ValidationError:
        return None
    if not cfg.ingest_folder:
        return None
    return OperatorRunSpec(
        cfg=cfg,
        folder=Path(cfg.ingest_folder),
        collection=CollectionId(cfg.ingest_collection),
    )


@dataclass(frozen=True)
class KbSearchRunSpec:
    """Параметры одного интеграционного прогона kb_search."""

    cfg: PostgresPluginConfig
    query: str
    top_k: int


@pytest.fixture
def kb_search_run() -> KbSearchRunSpec | None:
    """Operator-mode spec для kb_search. None, если плагин не настроен.

    Параметры запроса:

        BOBA_KB_SEARCH_QUERY=<строка>    # по умолчанию "test"
        BOBA_KB_SEARCH_TOP_K=<int>       # по умолчанию 5
    """
    try:
        cfg = PostgresPluginConfig()
    except ValidationError:
        return None
    return KbSearchRunSpec(
        cfg=cfg,
        query=os.environ.get("BOBA_KB_SEARCH_QUERY", "test"),
        top_k=int(os.environ.get("BOBA_KB_SEARCH_TOP_K", "5")),
    )
