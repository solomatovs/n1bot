"""Юнит-тесты fts_search/fts_list_indexes: callable → list/dict payload.

Tools конструируются напрямую (без AgentBuilder) и вызываются как обычные
callable-классы. DI-инжекция эмулируется передачей `kb=...`, `cfg=...`
kwargs'ами — это контракт `@tool`-классов после миграции на v2.
"""

from __future__ import annotations

from collections import namedtuple
from typing import Any

import pytest

from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tool.postgres_fts.fts_list_indexes import fts_list_indexes
from boba.tool.postgres_fts.fts_search import fts_search
from boba.tool.postgres_fts.models import IndexSpec

_Column = namedtuple("_Column", ["name"])


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], names: list[str]) -> None:
        self._rows = rows
        self.description = [_Column(n) for n in names]

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, stmt: Any, params: tuple[Any, ...]) -> None:
        del stmt, params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur

    def cursor(self) -> _FakeCursor:
        return self._cur


class _FakeCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *_: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]], names: list[str]) -> None:
        self._cur = _FakeCursor(rows, names)

    def connection(self) -> _FakeCM:
        return _FakeCM(_FakeConn(self._cur))


def _kb(rows: list[tuple[Any, ...]], names: list[str]) -> PgFtsKnowledgeBase:
    spec = IndexSpec(
        name="docs",
        description="Docs index",
        table="documents",
        id_column="id",
        tsv_column="tsv",
        snippet_column="body",
        schema="public",
        language="russian",
        metadata_columns=["title"],
    )
    return PgFtsKnowledgeBase(
        pool=_FakePool(rows, names),  # type: ignore[arg-type]
        indexes=[spec],
        snippet_options="MaxWords=20",
    )


def _cfg(max_top_k: int = 20) -> PostgresFtsPluginConfig:
    """Минимальный валидный конфиг для fts_search (enable=False допустим в тестах)."""
    return PostgresFtsPluginConfig.model_construct(
        enable=False,
        dsn="",
        indexes=[],
        max_top_k=max_top_k,
        snippet_options="MaxWords=20",
        min_pool_size=1,
        max_pool_size=4,
        connect_timeout_sec=10.0,
    )


def test_list_indexes_tool_returns_items():
    kb = _kb([], [])
    result = fts_list_indexes(kb=kb)
    assert result == [{"name": "docs", "description": "Docs index"}]


def test_search_tool_invokes_kb_and_serialises_hits():
    rows = [("1", 0.5, "snippet", "Title A")]
    kb = _kb(rows, ["_id", "_score", "_snippet", "title"])
    result = fts_search(
        index="docs", query="auth", kb=kb, cfg=_cfg(), top_k=3,
    )
    assert result == [
        {
            "id": "1",
            "score": 0.5,
            "metadata": {"title": "Title A"},
            "snippet": "snippet",
        },
    ]


def test_search_tool_uses_default_top_k_when_missing():
    kb = _kb([], ["_id", "_score", "_snippet"])
    result = fts_search(index="docs", query="x", kb=kb, cfg=_cfg())
    assert result == []


def test_search_tool_rejects_top_k_above_max():
    """В v2 top_k > max_top_k → RuntimeError (вместо v1 InvalidToolArgumentError).

    Pydantic-валидатор в v2 не имеет runtime-доступа к max_top_k (это поле
    конфига, не args-модели), поэтому проверка перенесена в тело tool'а.
    """
    kb = _kb([], ["_id", "_score", "_snippet"])
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        fts_search(
            index="docs", query="x", kb=kb, cfg=_cfg(max_top_k=5), top_k=100,
        )
