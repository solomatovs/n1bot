"""llm_view: плоская строка из канонических wire-ключей (1:1, без сведе́ния)."""

from __future__ import annotations

from boba.tool.kb.core.models import SearchHit
from boba.tool.kb.core.tools.search.llm_view import (
    build_link,
    flat_row,
    project_columns,
)

_COLUMNS = {
    "page_title",
    "source_url",
    "anchor",
    "page_id",
    "heading_path",
    "space",
}


def _hit(metadata: dict[str, str], tags: tuple[str, ...] = ()) -> SearchHit:
    return SearchHit(id="d:0", distance=0.1, metadata=metadata, snippet="s", tags=tags)


def test_canonical_wire_keys_map_to_columns() -> None:
    # одинаковые wire-ключи у kbdoc и confluence (всё — confluence-данные)
    hit = _hit(
        {
            "reader.page_title": "Правила именования",
            "source_url": "https://confl/viewpage?pageId=950276",
            "section.anchor": "backup-pitr",
            "confluence.page_id": "950276",
            "section.heading.path": "Backup › PITR",
            "confluence.space_key": "PAAS",
        },
        tags=("b", "a"),
    )
    out = project_columns(hit)
    assert set(out) == _COLUMNS | {"tags"}
    assert out["page_title"] == "Правила именования"
    assert out["source_url"] == "https://confl/viewpage?pageId=950276"
    assert out["anchor"] == "backup-pitr"
    assert out["page_id"] == "950276"
    assert out["heading_path"] == "Backup › PITR"
    assert out["space"] == "PAAS"
    assert out["tags"] == "a, b"  # стабильный порядок, из колонки tags


def test_missing_keys_become_empty() -> None:
    out = project_columns(_hit({"reader.page_title": "T"}))
    assert out["page_title"] == "T"
    assert out["source_url"] == ""
    assert out["tags"] == ""


def test_flat_row_builds_link_and_flat_columns() -> None:
    hit = _hit(
        {"source_url": "https://x", "section.anchor": "sec-1"}, tags=("t",),
    )
    row = flat_row(hit)
    assert row["id"] == "d:0"
    assert row["link"] == "https://x#sec-1"
    assert row["snippet"] == "s"
    assert _COLUMNS.issubset(row.keys())


def test_build_link_without_anchor() -> None:
    assert build_link({"source_url": "https://x"}) == "https://x"


def test_build_link_empty_when_no_source() -> None:
    assert build_link({"anchor": "a"}) == ""
