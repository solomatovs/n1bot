"""`confluence_list_spaces`: реальный list spaces от Apache cwiki."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.kb.confluence.tools.list.spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)

pytestmark = pytest.mark.integration


def _count_data_rows(table: str) -> int:
    """Сколько data-строк в markdown-таблице (без header'а, separator'а
    и truncated-маркера)."""
    return sum(
        1
        for line in table.splitlines()
        if line.startswith("|")
        and not line.startswith("| key |")
        and not line.startswith("| --- |")
    )


def test_returns_markdown_table_with_global_spaces(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """Реальный list spaces → markdown с header'ом и ≥1 data-строкой."""
    table = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="global", limit=50,
    )
    assert isinstance(table, str)
    assert table.startswith("| key | name | type | description |")
    assert "| --- | --- | --- | --- |" in table
    assert _count_data_rows(table) >= 1
    assert "AIRFLOW" in table


def test_limit_caps_rows_and_marks_truncated(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """`limit=N` обрезает до N data-строк и добавляет truncated-маркер."""
    table = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="global", limit=2,
    )
    assert _count_data_rows(table) == 2
    assert "more spaces omitted" in table


def test_type_any_returns_global_and_or_personal(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """`space_type="any"` снимает фильтр — должна вернуться ≥1 data-строка."""
    table = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="any", limit=10,
    )
    assert _count_data_rows(table) >= 1
