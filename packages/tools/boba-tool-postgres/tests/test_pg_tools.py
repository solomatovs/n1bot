"""Integration: `query` / `list_tables` / `describe_table`."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.pg.describe_table import DescribeTableConfig, describe_table
from boba.tool.pg.list_tables import ListTablesConfig, list_tables
from boba.tool.pg.query import QueryConfig, query

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# list_tables
# --------------------------------------------------------------------------- #


def test_list_tables_returns_markdown(
    list_tables_cfg: ListTablesConfig,
) -> None:
    """`list_tables` без schema-фильтра возвращает таблицы user-schema'ов."""
    result = list_tables(cfg=list_tables_cfg, schema=None)

    assert {"table", "row_count", "truncated"} <= result.keys()
    md = result["table"]
    assert isinstance(md, str)
    lines = md.splitlines()
    assert lines[0].startswith("|")
    assert "schema" in lines[0]
    assert lines[1].startswith("|")
    assert "---" in lines[1]


def test_list_tables_kb_chunks_visible(
    list_tables_cfg: ListTablesConfig,
) -> None:
    """В KB-БД должна быть `kb_chunks` (созданная bootstrap-миграцией)."""
    result = list_tables(cfg=list_tables_cfg, schema="public")
    assert "kb_chunks" in result["table"]


# --------------------------------------------------------------------------- #
# describe_table
# --------------------------------------------------------------------------- #


def test_describe_table_kb_chunks(
    describe_table_cfg: DescribeTableConfig,
) -> None:
    """Схема `kb_chunks` — ожидаемые системные колонки."""
    result = describe_table(
        cfg=describe_table_cfg,
        table="kb_chunks",
        schema="public",
    )
    assert result["schema"] == "public"
    assert result["table"] == "kb_chunks"
    assert result["column_count"] >= 5
    md = result["columns_table"]
    for col in ("chunk_id", "collection", "source_id", "embedding"):
        assert col in md


def test_describe_unknown_table_empty(
    describe_table_cfg: DescribeTableConfig,
) -> None:
    """Неизвестная таблица → пустой columns_table."""
    result = describe_table(
        cfg=describe_table_cfg,
        table="this_table_does_not_exist",
        schema="public",
    )
    assert result["column_count"] == 0
    assert "_(no rows)_" in result["columns_table"]


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


def test_query_simple_select(query_cfg: QueryConfig) -> None:
    """Простой `SELECT 1, 'hello'` → markdown с одной строкой."""
    result = query(
        cfg=query_cfg,
        sql="SELECT 1 AS n, 'hello' AS greeting",
        row_limit=5,
    )
    assert result["columns"] == ["n", "greeting"]
    assert result["row_count"] == 1
    assert not result["truncated"]
    md = result["table"]
    assert "| n | greeting |" in md
    assert "| 1 | hello |" in md


def test_query_count_kb_chunks(query_cfg: QueryConfig) -> None:
    """`SELECT count(*) FROM kb_chunks` — exploratory-запрос."""
    result = query(
        cfg=query_cfg,
        sql="SELECT count(*) AS chunks FROM kb_chunks",
        row_limit=1,
    )
    assert result["columns"] == ["chunks"]
    assert result["row_count"] == 1


def test_query_auto_limit_truncated(query_cfg: QueryConfig) -> None:
    """Много строк + малый row_limit → truncated=true."""
    result = query(
        cfg=query_cfg,
        sql="SELECT generate_series(1, 1000) AS n",
        row_limit=5,
    )
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["limit_applied"] == 5


# --------------------------------------------------------------------------- #
# Read-only guard (PG-side)
# --------------------------------------------------------------------------- #


def test_query_readonly_blocks_insert(query_cfg: QueryConfig) -> None:
    """INSERT ловится PG-side."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            sql="INSERT INTO kb_chunks (chunk_id) VALUES ('x')",
            row_limit=1,
        )


def test_query_readonly_blocks_update(query_cfg: QueryConfig) -> None:
    """UPDATE — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            sql="UPDATE kb_chunks SET chunk_id = chunk_id",
            row_limit=1,
        )


def test_query_readonly_blocks_drop(query_cfg: QueryConfig) -> None:
    """DROP — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            sql="DROP TABLE kb_chunks",
            row_limit=1,
        )
