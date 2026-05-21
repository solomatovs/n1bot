"""`confluence_list_spaces`: реальный list spaces от Apache cwiki.

Integration: тулза дёргает `/rest/api/space` на `[tool.kb.confluence].base_url`
с пагинацией и возвращает markdown-таблицу. На Apache cwiki публично
доступны ≥ десятки global-spaces (включая `KAFKA`), что и проверяем.

`@tool`-декоратор не оборачивает функцию (только маркер-атрибут), так
что вызываем `confluence_list_spaces(conn_cfg=..., ...)` напрямую,
передавая FromConfig-аргумент руками.
"""

from __future__ import annotations

import pytest

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.list_spaces import confluence_list_spaces

pytestmark = pytest.mark.integration


def test_returns_markdown_table_with_global_spaces(
    confluence_cfg: ConfluenceConnectionConfig,
) -> None:
    """Реальный list spaces → markdown с header'ом и ≥1 строкой."""
    out = confluence_list_spaces(
        conn_cfg=confluence_cfg, space_type="global", limit=50,
    )
    table = out["table"]
    assert isinstance(table, str)
    assert table.startswith("| key | name | type | description |")
    assert "| --- | --- | --- | --- |" in table
    assert out["row_count"] >= 1
    # cwiki сортирует spaces по name alphabetically; в первых ~50 заведомо
    # есть `AIRFLOW`. Если оператор сменит порядок (вряд ли) — упадёт
    # явно, и придётся пересмотреть assert.
    assert "AIRFLOW" in table


def test_limit_caps_rows_and_marks_truncated(
    confluence_cfg: ConfluenceConnectionConfig,
) -> None:
    """`limit=N` обрезает до N и помечает `truncated=True`, если ещё есть."""
    out = confluence_list_spaces(
        conn_cfg=confluence_cfg, space_type="global", limit=2,
    )
    assert out["row_count"] == 2
    assert out["truncated"] is True
    assert "more spaces omitted" in out["table"]


def test_type_any_returns_global_and_or_personal(
    confluence_cfg: ConfluenceConnectionConfig,
) -> None:
    """`space_type="any"` снимает фильтр — должны вернуться ≥ 1 строка.

    На cwiki personal spaces могут быть disabled; здесь главное — что
    tool не валится при type="any" и возвращает ≥1 запись.
    """
    out = confluence_list_spaces(
        conn_cfg=confluence_cfg, space_type="any", limit=10,
    )
    assert out["row_count"] >= 1
