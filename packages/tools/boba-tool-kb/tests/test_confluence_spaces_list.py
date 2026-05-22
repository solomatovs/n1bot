"""`confluence_spaces_list`: реальный list spaces от Apache cwiki."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.kb.confluence.tools.spaces_list import (
    ConfluenceSpacesListConfig,
    confluence_spaces_list,
)

pytestmark = pytest.mark.integration


def test_returns_markdown_table_with_global_spaces(
    confluence_spaces_list_cfg: ConfluenceSpacesListConfig,
) -> None:
    """Реальный list spaces → markdown с header'ом и ≥1 строкой."""
    out = confluence_spaces_list(
        cfg=confluence_spaces_list_cfg, space_type="global", limit=50,
    )
    table = out["table"]
    assert isinstance(table, str)
    assert table.startswith("| key | name | type | description |")
    assert "| --- | --- | --- | --- |" in table
    assert out["row_count"] >= 1
    assert "AIRFLOW" in table


def test_limit_caps_rows_and_marks_truncated(
    confluence_spaces_list_cfg: ConfluenceSpacesListConfig,
) -> None:
    """`limit=N` обрезает до N и помечает `truncated=True`, если ещё есть."""
    out = confluence_spaces_list(
        cfg=confluence_spaces_list_cfg, space_type="global", limit=2,
    )
    assert out["row_count"] == 2
    assert out["truncated"] is True
    assert "more spaces omitted" in out["table"]


def test_type_any_returns_global_and_or_personal(
    confluence_spaces_list_cfg: ConfluenceSpacesListConfig,
) -> None:
    """`space_type="any"` снимает фильтр — должны вернуться ≥ 1 строка."""
    out = confluence_spaces_list(
        cfg=confluence_spaces_list_cfg, space_type="any", limit=10,
    )
    assert out["row_count"] >= 1
