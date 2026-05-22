"""Integration: `sql_query` / `sql_list_tables` / `sql_describe_table`."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.kb.sql.tools.describe_table import (
    SqlDescribeTableConfig,
    sql_describe_table,
)
from boba.tool.kb.sql.tools.list_tables import SqlListTablesConfig, sql_list_tables
from boba.tool.kb.sql.tools.query import SqlQueryConfig, sql_query

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# sql_list_tables
# --------------------------------------------------------------------------- #


def test_sql_list_tables_returns_markdown(
    sql_list_tables_cfg: SqlListTablesConfig,
) -> None:
    """`sql_list_tables` без schema-фильтра возвращает таблицы user-schema'ов."""
    result = sql_list_tables(cfg=sql_list_tables_cfg, schema=None)

    assert {"table", "row_count", "truncated"} <= result.keys()
    md = result["table"]
    assert isinstance(md, str)
    lines = md.splitlines()
    assert lines[0].startswith("|")
    assert "schema" in lines[0]
    assert lines[1].startswith("|")
    assert "---" in lines[1]


def test_sql_list_tables_kb_chunks_visible(
    sql_list_tables_cfg: SqlListTablesConfig,
) -> None:
    """В KB-БД должна быть `kb_chunks` (созданная bootstrap-миграцией)."""
    result = sql_list_tables(cfg=sql_list_tables_cfg, schema="public")
    assert "kb_chunks" in result["table"]


# --------------------------------------------------------------------------- #
# sql_describe_table
# --------------------------------------------------------------------------- #


def test_sql_describe_table_kb_chunks(
    sql_describe_table_cfg: SqlDescribeTableConfig,
) -> None:
    """Схема `kb_chunks` — ожидаемые системные колонки."""
    result = sql_describe_table(
        cfg=sql_describe_table_cfg,
        table="kb_chunks",
        schema="public",
    )
    assert result["schema"] == "public"
    assert result["table"] == "kb_chunks"
    assert result["column_count"] >= 5
    md = result["columns_table"]
    for col in ("chunk_id", "collection", "source_id", "embedding"):
        assert col in md


def test_sql_describe_unknown_table_empty(
    sql_describe_table_cfg: SqlDescribeTableConfig,
) -> None:
    """Неизвестная таблица → пустой columns_table."""
    result = sql_describe_table(
        cfg=sql_describe_table_cfg,
        table="this_table_does_not_exist",
        schema="public",
    )
    assert result["column_count"] == 0
    assert "_(no rows)_" in result["columns_table"]


# --------------------------------------------------------------------------- #
# sql_query
# --------------------------------------------------------------------------- #


def test_sql_query_simple_select(sql_query_cfg: SqlQueryConfig) -> None:
    """Простой `SELECT 1, 'hello'` → markdown с одной строкой."""
    result = sql_query(
        cfg=sql_query_cfg,
        query="SELECT 1 AS n, 'hello' AS greeting",
        row_limit=5,
    )
    assert result["columns"] == ["n", "greeting"]
    assert result["row_count"] == 1
    assert not result["truncated"]
    md = result["table"]
    assert "| n | greeting |" in md
    assert "| 1 | hello |" in md


def test_sql_query_count_kb_chunks(sql_query_cfg: SqlQueryConfig) -> None:
    """`SELECT count(*) FROM kb_chunks` — exploratory-запрос."""
    result = sql_query(
        cfg=sql_query_cfg,
        query="SELECT count(*) AS chunks FROM kb_chunks",
        row_limit=1,
    )
    assert result["columns"] == ["chunks"]
    assert result["row_count"] == 1


def test_sql_query_auto_limit_truncated(sql_query_cfg: SqlQueryConfig) -> None:
    """Много строк + малый row_limit → truncated=true."""
    result = sql_query(
        cfg=sql_query_cfg,
        query="SELECT generate_series(1, 1000) AS n",
        row_limit=5,
    )
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["limit_applied"] == 5


# --------------------------------------------------------------------------- #
# Read-only guard (PG-side)
# --------------------------------------------------------------------------- #


def test_sql_query_readonly_blocks_insert(sql_query_cfg: SqlQueryConfig) -> None:
    """INSERT ловится PG-side."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            cfg=sql_query_cfg,
            query="INSERT INTO kb_chunks (chunk_id) VALUES ('x')",
            row_limit=1,
        )


def test_sql_query_readonly_blocks_update(sql_query_cfg: SqlQueryConfig) -> None:
    """UPDATE — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            cfg=sql_query_cfg,
            query="UPDATE kb_chunks SET chunk_id = chunk_id",
            row_limit=1,
        )


def test_sql_query_readonly_blocks_drop(sql_query_cfg: SqlQueryConfig) -> None:
    """DROP — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            cfg=sql_query_cfg,
            query="DROP TABLE kb_chunks",
            row_limit=1,
        )
