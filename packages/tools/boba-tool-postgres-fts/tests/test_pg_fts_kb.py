"""Юнит-тесты PgFtsKnowledgeBase: маппинг rows→FtsHit, IndexNotFoundError, SQL-shape."""

from __future__ import annotations

from collections import namedtuple
from typing import Any

import pytest

from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tool.postgres_fts.errors import IndexNotFoundError
from boba.tool.postgres_fts.models import IndexSpec
from boba.tools.domain import ToolName, ToolSourceId, compose_tool_id

_TOOL_ID = compose_tool_id(ToolSourceId("plugin_postgres_fts"), ToolName("fts_search"))
_Column = namedtuple("_Column", ["name"])


class _FakeCursor:
    """Минимальный stub для psycopg-курсора с захватом execute-args."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        column_names: list[str],
    ) -> None:
        self._rows = rows
        self.description = [_Column(n) for n in column_names]
        self.executed_stmt: Any = None
        self.executed_params: tuple[Any, ...] = ()

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, stmt: Any, params: tuple[Any, ...]) -> None:
        self.executed_stmt = stmt
        self.executed_params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _FakeConnectionCM:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConnection:
        return self._conn

    def __exit__(self, *_: object) -> None:
        return None


class _FakePool:
    """Подсовываемый PgFtsKnowledgeBase pool без psycopg."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        column_names: list[str],
    ) -> None:
        self.cursor = _FakeCursor(rows, column_names)

    def connection(self) -> _FakeConnectionCM:
        return _FakeConnectionCM(_FakeConnection(self.cursor))


def _spec(
    name: str = "docs",
    *,
    metadata_columns: list[str] | None = None,
) -> IndexSpec:
    return IndexSpec(
        name=name,
        description=f"Test index {name}",
        table="documents",
        id_column="id",
        tsv_column="tsv",
        snippet_column="body",
        schema="public",
        language="russian",
        metadata_columns=metadata_columns or [],
    )


def test_list_indexes_returns_whitelist():
    pool = _FakePool([], [])
    kb = PgFtsKnowledgeBase(
        pool=pool,  # type: ignore[arg-type]
        indexes=[_spec("docs"), _spec("tickets")],
        snippet_options="MaxWords=20",
    )
    names = [i.name for i in kb.list_indexes()]
    assert names == ["docs", "tickets"]


def test_search_unknown_index_raises():
    pool = _FakePool([], [])
    kb = PgFtsKnowledgeBase(
        pool=pool,  # type: ignore[arg-type]
        indexes=[_spec("docs")],
        snippet_options="MaxWords=20",
    )
    with pytest.raises(IndexNotFoundError):
        kb.search(_TOOL_ID, "missing", "q", 5)


def test_search_maps_rows_to_hits_with_metadata():
    rows = [
        ("42", 0.91, "hit <b>one</b>", "Title A", "https://a"),
        ("43", 0.42, "hit two", None, "https://b"),
    ]
    columns = ["_id", "_score", "_snippet", "title", "source_url"]
    pool = _FakePool(rows, columns)
    kb = PgFtsKnowledgeBase(
        pool=pool,  # type: ignore[arg-type]
        indexes=[_spec("docs", metadata_columns=["title", "source_url"])],
        snippet_options="MaxWords=20",
    )

    hits = kb.search(_TOOL_ID, "docs", "anything", 5)

    assert [h.id for h in hits] == ["42", "43"]
    assert [h.score for h in hits] == [0.91, 0.42]
    assert hits[0].snippet == "hit <b>one</b>"
    assert hits[0].metadata == {"title": "Title A", "source_url": "https://a"}
    # None-значения отфильтрованы.
    assert hits[1].metadata == {"source_url": "https://b"}


def test_search_passes_query_and_top_k_as_params():
    pool = _FakePool([], ["_id", "_score", "_snippet"])
    kb = PgFtsKnowledgeBase(
        pool=pool,  # type: ignore[arg-type]
        indexes=[_spec("docs")],
        snippet_options="MaxFragments=1",
    )
    kb.search(_TOOL_ID, "docs", "auth tokens", 7)

    params = pool.cursor.executed_params
    # Порядок: language, snippet_options, language, query, top_k.
    assert params == ("russian", "MaxFragments=1", "russian", "auth tokens", 7)


def test_search_sql_contains_websearch_and_rank():
    # SQL composable ещё не материализован в строку — but as_string доступен
    # через psycopg.sql.Composed.__str__ для отладки.
    pool = _FakePool([], ["_id", "_score", "_snippet"])
    kb = PgFtsKnowledgeBase(
        pool=pool,  # type: ignore[arg-type]
        indexes=[_spec("docs")],
        snippet_options="MaxWords=20",
    )
    kb.search(_TOOL_ID, "docs", "q", 5)

    rendered = str(pool.cursor.executed_stmt)
    assert "websearch_to_tsquery" in rendered
    assert "ts_rank_cd" in rendered
    assert "ts_headline" in rendered
