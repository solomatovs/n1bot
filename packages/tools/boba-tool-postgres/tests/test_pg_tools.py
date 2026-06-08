"""Integration: query / list_tables / describe_table."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.pg.describe_table import describe_table
from boba.tool.pg.executor import SqlExecutorConfig
from boba.tool.pg.list_tables import list_tables
from boba.tool.pg.query import query
from boba.tools.domain import ErrorResult, PgCopyTextResult, TableResult

pytestmark = pytest.mark.integration

TARGET = "main"


def _header(result: PgCopyTextResult) -> list[str | None]:
    return next(iter(result.iter_rows()), [])


def _data_rows(result: PgCopyTextResult) -> list[list[str | None]]:
    """data-строки (без header) через доменный TEXT-парсер."""
    return list(result.iter_rows())[1:]


# --------------------------------------------------------------------------- #
# list_tables
# --------------------------------------------------------------------------- #


def test_list_tables_returns_table(
    list_tables_cfg: SqlExecutorConfig,
) -> None:
    """list_tables без schema-фильтра -> TableResult с колонками schema/…."""
    res = list_tables(cfg=list_tables_cfg, target=TARGET, pg_schema=None)
    assert isinstance(res, TableResult)
    assert res.rows
    assert "schema" in res.rows[0]


def test_list_tables_kb_chunks_visible(
    list_tables_cfg: SqlExecutorConfig,
) -> None:
    """В KB-БД должна быть kb_chunks (созданная bootstrap-миграцией)."""
    res = list_tables(cfg=list_tables_cfg, target=TARGET, pg_schema="public")
    assert isinstance(res, TableResult)
    assert any("kb_chunks" in str(v) for row in res.rows for v in row.values())


# --------------------------------------------------------------------------- #
# describe_table
# --------------------------------------------------------------------------- #


def test_describe_table_kb_chunks(
    describe_table_cfg: SqlExecutorConfig,
) -> None:
    """Схема kb_chunks — ожидаемые системные колонки."""
    res = describe_table(
        cfg=describe_table_cfg,
        target=TARGET,
        table="kb_chunks",
        pg_schema="public",
    )
    assert isinstance(res, TableResult)
    assert len(res.rows) >= 5
    names = {row["column_name"] for row in res.rows}
    assert {"chunk_id", "collection", "source_id", "embedding"} <= names


def test_describe_unknown_table_empty(
    describe_table_cfg: SqlExecutorConfig,
) -> None:
    """Неизвестная таблица -> TableResult без строк."""
    res = describe_table(
        cfg=describe_table_cfg,
        target=TARGET,
        table="this_table_does_not_exist",
        pg_schema="public",
    )
    assert isinstance(res, TableResult)
    assert res.rows == []


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


def test_query_simple_select(query_cfg: SqlExecutorConfig) -> None:
    """Простой SELECT 1, 'hello' -> PgCopyTextResult с одной data-строкой."""
    res = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT 1 AS n, 'hello' AS greeting",
    )
    assert isinstance(res, PgCopyTextResult)
    assert _header(res) == ["n", "greeting"]
    assert _data_rows(res) == [["1", "hello"]]


def test_query_count_kb_chunks(query_cfg: SqlExecutorConfig) -> None:
    """SELECT count(*) FROM kb_chunks — exploratory-запрос."""
    res = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT count(*) AS chunks FROM kb_chunks",
    )
    assert isinstance(res, PgCopyTextResult)
    assert _header(res) == ["chunks"]
    assert len(_data_rows(res)) == 1


def test_query_too_many_rows(query_cfg: SqlExecutorConfig) -> None:
    """Строк больше max_rows -> ErrorResult «добавьте LIMIT» (запрос не трогаем)."""
    res = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT generate_series(1, 1000) AS n",
    )
    assert isinstance(res, ErrorResult)
    assert res.error_kind == "too_many_rows"


def test_query_with_limit_ok(query_cfg: SqlExecutorConfig) -> None:
    """С LIMIT в самом запросе — успешный PgCopyTextResult."""
    res = query(
        cfg=query_cfg,
        target=TARGET,
        sql="SELECT generate_series(1, 5) AS n",
    )
    assert isinstance(res, PgCopyTextResult)
    assert len(_data_rows(res)) == 5


# --------------------------------------------------------------------------- #
# DML/DDL guard (PG-side)
# --------------------------------------------------------------------------- #
#
# Через COPY (<query>) TO STDOUT не-SELECT отбивается PG: либо read-only
# transaction, либо «COPY query must have a RETURNING clause» — порядок
# проверок PG-зависим, поэтому фиксируем сам факт ошибки (RuntimeError),
# а не текст.


def test_query_blocks_insert(query_cfg: SqlExecutorConfig) -> None:
    """INSERT не проходит через query."""
    with pytest.raises(RuntimeError):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="INSERT INTO kb_chunks (chunk_id) VALUES ('x')",
        )


def test_query_blocks_update(query_cfg: SqlExecutorConfig) -> None:
    """UPDATE не проходит через query."""
    with pytest.raises(RuntimeError):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="UPDATE kb_chunks SET chunk_id = chunk_id",
        )


def test_query_blocks_drop(query_cfg: SqlExecutorConfig) -> None:
    """DROP не проходит через query."""
    with pytest.raises(RuntimeError):
        query(
            cfg=query_cfg,
            target=TARGET,
            sql="DROP TABLE kb_chunks",
        )
