"""Прямой прогон операций инструментов: класс payload'а вызывается как есть.

Тест собирает запрос протокола, отдаёт его классу операций (PostgresOps,
ClickHouseOps, WebOps, DocumentOps, KbOps, ConfluenceOps, IngestOps, ChartOps)
и получает кадры данных плюс трейлер. Ни песочницы, ни langchain-обёртки,
ни исполнителя порта в этом пути нет.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from omegaconf import DictConfig
from pydantic import BaseModel

from boba.chainlit.infra.entry import AppEntry
from boba.settings import build_app_config
from boba.toolkit.launcher import RowStream


class ChunkLog:
    """Кадры операции: write передаётся ей как ChunkEmitter."""

    def __init__(self) -> None:
        self._chunks: list[str] = []

    def write(self, chunk: str) -> None:
        self._chunks.append(chunk)

    @property
    def chunks(self) -> Sequence[str]:
        return tuple(self._chunks)

    def text(self) -> str:
        """Текстовый поток: куски склеиваются в исходный текст."""
        return "".join(self._chunks)

    def rows(self) -> list[dict[str, Any]]:
        """Строчный поток: каждый кадр — одна NDJSON-запись."""
        rows: list[dict[str, Any]] = []
        for chunk in self._chunks:
            rows.append(RowStream.decode(chunk))

        return rows


class Payload:
    """Запрос операции: модель протокола -> тот же JSON, что уходит в stdin."""

    @staticmethod
    def of(request: BaseModel) -> dict[str, Any]:
        return json.loads(request.model_dump_json())


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Конфиг приложения: BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE."""
    return build_app_config(config_path=AppEntry.config_path())


@pytest.fixture
def chunks() -> ChunkLog:
    return ChunkLog()


@pytest.fixture(scope="session")
def payload() -> type[Payload]:
    return Payload
