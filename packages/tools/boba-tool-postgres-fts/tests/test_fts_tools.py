"""Юнит-тесты FtsSearchTool/FtsListIndexesTool: invoke → JsonResult."""

from __future__ import annotations

from collections import namedtuple
from typing import Any

import pytest

from boba.plugin.prompt import PromptOverlay
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tool.postgres_fts.fts_list_indexes import (
    FtsListIndexesTool,
    FtsListIndexesToolConfig,
)
from boba.tool.postgres_fts.fts_search import FtsSearchTool, FtsSearchToolConfig
from boba.tool.postgres_fts.models import IndexSpec
from boba.tools.domain import JsonResult, ToolContext, ToolSourceId
from boba.tools.domain.errors import InvalidToolArgumentError

_SOURCE = ToolSourceId("plugin_postgres_fts")
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


def test_list_indexes_tool_returns_json_payload():
    kb = _kb([], [])
    tool = FtsListIndexesTool(
        kb,
        FtsListIndexesToolConfig(prompt=PromptOverlay()),
        ctx=None,
        source_id=_SOURCE,
    )
    result = tool.invoke(ToolContext(), {})
    assert isinstance(result, JsonResult)
    assert result.payload == [{"name": "docs", "description": "Docs index"}]


def test_search_tool_invokes_kb_and_serialises_hits():
    rows = [("1", 0.5, "snippet", "Title A")]
    kb = _kb(rows, ["_id", "_score", "_snippet", "title"])
    tool = FtsSearchTool(
        kb,
        FtsSearchToolConfig(max_top_k=20, prompt=PromptOverlay()),
        ctx=None,
        source_id=_SOURCE,
    )
    result = tool.invoke(
        ToolContext(),
        {"index": "docs", "query": "auth", "top_k": 3},
    )
    assert isinstance(result, JsonResult)
    assert result.payload == [
        {
            "id": "1",
            "score": 0.5,
            "metadata": {"title": "Title A"},
            "snippet": "snippet",
        },
    ]


def test_search_tool_uses_default_top_k_when_missing():
    kb = _kb([], ["_id", "_score", "_snippet"])
    tool = FtsSearchTool(
        kb,
        FtsSearchToolConfig(max_top_k=20, prompt=PromptOverlay()),
        ctx=None,
        source_id=_SOURCE,
    )
    result = tool.invoke(ToolContext(), {"index": "docs", "query": "x"})
    assert isinstance(result, JsonResult)


def test_search_tool_rejects_top_k_above_max():
    kb = _kb([], ["_id", "_score", "_snippet"])
    tool = FtsSearchTool(
        kb,
        FtsSearchToolConfig(max_top_k=5, prompt=PromptOverlay()),
        ctx=None,
        source_id=_SOURCE,
    )
    with pytest.raises(InvalidToolArgumentError):
        tool.invoke(ToolContext(), {"index": "docs", "query": "x", "top_k": 100})


def test_search_tool_rejects_missing_required_arg():
    kb = _kb([], ["_id", "_score", "_snippet"])
    tool = FtsSearchTool(
        kb,
        FtsSearchToolConfig(max_top_k=5, prompt=PromptOverlay()),
        ctx=None,
        source_id=_SOURCE,
    )
    with pytest.raises(InvalidToolArgumentError):
        tool.invoke(ToolContext(), {"query": "x"})
