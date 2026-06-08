"""confluence_list_spaces: реальный list spaces от Apache cwiki."""
# pyright: reportCallIssue=false

from __future__ import annotations

import pytest

from boba.tool.kb.confluence.list_spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tools.domain import TableResult

pytestmark = pytest.mark.integration


def test_returns_table_with_global_spaces(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """Реальный list spaces -> TableResult со строками {key, name, type}."""
    res = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="global", limit=50,
    )
    assert isinstance(res, TableResult)
    assert len(res.rows) >= 1
    assert set(res.rows[0]) == {"key", "name", "type"}


def test_pattern_filters_by_glob(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """pattern="airflow*" находит space AIRFLOW по key/name (glob-фильтр)."""
    res = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, pattern="airflow*", space_type="global",
        limit=50,
    )
    keys = {row["key"] for row in res.rows}
    assert "AIRFLOW" in keys


def test_limit_caps_rows_and_sets_note(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """limit=N обрезает до N строк и проставляет truncated-note."""
    res = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="global", limit=2,
    )
    assert len(res.rows) == 2
    assert res.note is not None


def test_type_any_returns_global_and_or_personal(
    confluence_list_spaces_cfg: ConfluenceListSpacesConfig,
) -> None:
    """space_type="any" снимает фильтр — должна вернуться ≥1 строка."""
    res = confluence_list_spaces(
        cfg=confluence_list_spaces_cfg, space_type="any", limit=10,
    )
    assert len(res.rows) >= 1
