"""Integration: `sql_query` / `sql_list_tables` / `sql_describe_table`.

Реальный Postgres через `[tool.kb.sql]`-секцию (отдельная от
`[tool.kb.postgres]`, чтобы можно было дать LLM ограниченную read-only
роль). Тесты проверяют:

- shape ответа (markdown table + meta-поля).
- `SET LOCAL default_transaction_read_only=on` реально блокирует
  INSERT/DROP/etc на PG-side (ловится как SqlQueryError).
- markdown-форматирование: header + separator + rows.
"""

from __future__ import annotations

import pytest

from boba.tool.kb.sql.executor import SqlExecutor
from boba.tool.kb.sql.sql_describe_table import sql_describe_table
from boba.tool.kb.sql.sql_list_tables import sql_list_tables
from boba.tool.kb.sql.sql_query import sql_query

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# sql_list_tables
# --------------------------------------------------------------------------- #


def test_sql_list_tables_returns_markdown(sql_executor: SqlExecutor) -> None:
    """`sql_list_tables` без schema-фильтра возвращает таблицы user-schema'ов."""
    result = sql_list_tables(executor=sql_executor, schema=None)

    assert {"table", "row_count", "truncated"} <= result.keys()
    md = result["table"]
    assert isinstance(md, str)
    # markdown-table: первая строка — header `| schema | table | kind |`
    lines = md.splitlines()
    assert lines[0].startswith("|")
    assert "schema" in lines[0]
    assert lines[1].startswith("|")
    assert "---" in lines[1]


def test_sql_list_tables_kb_chunks_visible(sql_executor: SqlExecutor) -> None:
    """В KB-БД должна быть `kb_chunks` (созданная bootstrap-миграцией)."""
    result = sql_list_tables(executor=sql_executor, schema="public")
    assert "kb_chunks" in result["table"], (
        f"kb_chunks не виден через sql_list_tables(schema=public): "
        f"{result['table']!r}"
    )


# --------------------------------------------------------------------------- #
# sql_describe_table
# --------------------------------------------------------------------------- #


def test_sql_describe_table_kb_chunks(sql_executor: SqlExecutor) -> None:
    """Схема `kb_chunks` — ожидаемые системные колонки."""
    result = sql_describe_table(
        table="kb_chunks",
        executor=sql_executor,
        schema="public",
    )
    assert result["schema"] == "public"
    assert result["table"] == "kb_chunks"
    assert result["column_count"] >= 5  # есть как минимум system-fields
    md = result["columns_table"]
    # Известные системные поля kb_chunks:
    for col in ("chunk_id", "collection", "source_id", "embedding"):
        assert col in md, f"колонка {col!r} отсутствует в описании kb_chunks"


def test_sql_describe_unknown_table_empty(sql_executor: SqlExecutor) -> None:
    """Неизвестная таблица → пустой columns_table (information_schema пустая)."""
    result = sql_describe_table(
        table="this_table_does_not_exist",
        executor=sql_executor,
        schema="public",
    )
    assert result["column_count"] == 0
    assert "_(no rows)_" in result["columns_table"]


# --------------------------------------------------------------------------- #
# sql_query: shape + LIMIT-injection
# --------------------------------------------------------------------------- #


def test_sql_query_simple_select(sql_executor: SqlExecutor) -> None:
    """Простой `SELECT 1, 'hello'` → markdown с одной строкой."""
    result = sql_query(
        query="SELECT 1 AS n, 'hello' AS greeting",
        executor=sql_executor,
        row_limit=5,
    )
    assert result["columns"] == ["n", "greeting"]
    assert result["row_count"] == 1
    assert not result["truncated"]
    md = result["table"]
    assert "| n | greeting |" in md
    assert "| 1 | hello |" in md


def test_sql_query_count_kb_chunks(sql_executor: SqlExecutor) -> None:
    """`SELECT count(*) FROM kb_chunks` — стандартный exploratory-запрос."""
    result = sql_query(
        query="SELECT count(*) AS chunks FROM kb_chunks",
        executor=sql_executor,
        row_limit=1,
    )
    assert result["columns"] == ["chunks"]
    assert result["row_count"] == 1


def test_sql_query_auto_limit_truncated(sql_executor: SqlExecutor) -> None:
    """Если в SELECT много строк и нет LIMIT — auto-injection + truncated=true."""
    result = sql_query(
        query="SELECT generate_series(1, 1000) AS n",
        executor=sql_executor,
        row_limit=5,
    )
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["limit_applied"] == 5


# --------------------------------------------------------------------------- #
# Read-only guard (PG-side). Защита keyword-валидатора удалена; единственный
# гард — `SET LOCAL default_transaction_read_only=on` + права DSN-роли.
# --------------------------------------------------------------------------- #


def test_sql_query_readonly_blocks_insert(sql_executor: SqlExecutor) -> None:
    """INSERT ловится PG-side как `cannot execute INSERT in a read-only transaction`."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            query="INSERT INTO kb_chunks (chunk_id) VALUES ('x')",
            executor=sql_executor,
            row_limit=1,
        )


def test_sql_query_readonly_blocks_update(sql_executor: SqlExecutor) -> None:
    """UPDATE — тоже PG-side reject."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            query="UPDATE kb_chunks SET chunk_id = chunk_id",
            executor=sql_executor,
            row_limit=1,
        )


def test_sql_query_readonly_blocks_drop(sql_executor: SqlExecutor) -> None:
    """DROP — тоже PG-side reject (даже у суперюзера в read-only транзакции)."""
    with pytest.raises(RuntimeError, match=r"read-only|permission"):
        sql_query(
            query="DROP TABLE kb_chunks",
            executor=sql_executor,
            row_limit=1,
        )
