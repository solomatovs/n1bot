"""Integration: `query` / `list_tables` / `describe_table`."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.pg.describe_table import DescribeTableConfig, describe_table
from boba.tool.pg.list_tables import ListTablesConfig, list_tables
from boba.tool.pg.query import QueryConfig, query

pytestmark = pytest.mark.integration

TARGET = "main"


def _count_data_rows(md: str) -> int:
    """Сколько data-строк в markdown-таблице.

    Считает только `|`-строки после header'а + separator'а, исключая
    `_(no rows)_`-заглушку. Truncated-маркер не `|`-строка, не учитывается.
    """
    lines = md.splitlines()
    return sum(
        1
        for line in lines[2:]
        if line.startswith("|") and "_(no rows)_" not in line
    )


# --------------------------------------------------------------------------- #
# list_tables
# --------------------------------------------------------------------------- #


def test_list_tables_returns_markdown(
    list_tables_cfg: ListTablesConfig,
) -> None:
    """`list_tables` без schema-фильтра возвращает таблицы user-schema'ов."""
    md = list_tables(cfg=list_tables_cfg, target=TARGET, schema=None)
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
    md = list_tables(cfg=list_tables_cfg, target=TARGET, schema="public")
    assert "kb_chunks" in md


# --------------------------------------------------------------------------- #
# describe_table
# --------------------------------------------------------------------------- #


def test_describe_table_kb_chunks(
    describe_table_cfg: DescribeTableConfig,
) -> None:
    """Схема `kb_chunks` — ожидаемые системные колонки."""
    md = describe_table(
        cfg=describe_table_cfg,
        target=TARGET,
        table="kb_chunks",
        schema="public",
    )
    assert isinstance(md, str)
    assert _count_data_rows(md) >= 5
    for col in ("chunk_id", "collection", "source_id", "embedding"):
        assert col in md


def test_describe_unknown_table_empty(
    describe_table_cfg: DescribeTableConfig,
) -> None:
    """Неизвестная таблица → markdown с `_(no rows)_`-заглушкой."""
    md = describe_table(
        cfg=describe_table_cfg,
        target=TARGET,
        table="this_table_does_not_exist",
        schema="public",
    )
    assert _count_data_rows(md) == 0
    assert "_(no rows)_" in md


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


def test_query_simple_select(query_cfg: QueryConfig) -> None:
    """Простой `SELECT 1, 'hello'` → markdown с одной строкой."""
    md = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT 1 AS n, 'hello' AS greeting",
        row_limit=5,
    )
    assert isinstance(md, str)
    assert "| n | greeting |" in md
    assert "| 1 | hello |" in md
    assert _count_data_rows(md) == 1
    assert "more rows omitted" not in md


def test_query_count_kb_chunks(query_cfg: QueryConfig) -> None:
    """`SELECT count(*) FROM kb_chunks` — exploratory-запрос."""
    md = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT count(*) AS chunks FROM kb_chunks",
        row_limit=1,
    )
    assert "| chunks |" in md
    assert _count_data_rows(md) == 1


def test_query_auto_limit_truncated(query_cfg: QueryConfig) -> None:
    """Много строк + малый row_limit → truncated-маркер в markdown."""
    md = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT generate_series(1, 1000) AS n",
        row_limit=5,
    )
    assert _count_data_rows(md) == 5
    assert "more rows omitted" in md


# --------------------------------------------------------------------------- #
# Read-only guard (PG-side)
# --------------------------------------------------------------------------- #


def test_query_readonly_blocks_insert(query_cfg: QueryConfig) -> None:
    """INSERT ловится PG-side."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="INSERT INTO kb_chunks (chunk_id) VALUES ('x')",
            row_limit=1,
        )


def test_query_readonly_blocks_update(query_cfg: QueryConfig) -> None:
    """UPDATE — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="UPDATE kb_chunks SET chunk_id = chunk_id",
            row_limit=1,
        )


def test_query_readonly_blocks_drop(query_cfg: QueryConfig) -> None:
    """DROP — PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="DROP TABLE kb_chunks",
            row_limit=1,
        )
