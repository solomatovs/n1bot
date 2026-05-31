"""CollectionSearch: явная сборка строки из kb_chunks + пересечение с ingest."""

from __future__ import annotations

from boba.indexing import ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.core.models import SearchHit
from boba.tool.kb.core.search import (
    ConfluenceCollection,
    KbDocCollection,
)

_META_COLUMNS = {
    "page_title",
    "source_url",
    "anchor",
    "page_id",
    "heading_path",
    "space",
}
_COLUMN_FIELDS = {"id", "distance", "snippet", "tags", "link"}


def _hit(metadata: dict[str, str], tags: tuple[str, ...] = ()) -> SearchHit:
    return SearchHit(id="d:0", distance=0.1, metadata=metadata, snippet="s", tags=tags)


def test_collections_are_strict_and_distinct() -> None:
    assert ConfluenceCollection.COLLECTION == "kb_confluence"
    assert KbDocCollection.COLLECTION == "kb_confluence_doc"


def test_row_assembles_columns_and_metadata() -> None:
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
    row = ConfluenceCollection.row(hit)
    # прямые колонки kb_chunks
    assert row["id"] == "d:0"
    assert row["distance"] == 0.1
    assert row["snippet"] == "s"
    assert row["tags"] == "a, b"
    assert row["link"] == "https://confl/viewpage?pageId=950276#backup-pitr"
    # поля из metadata
    assert _META_COLUMNS.issubset(row.keys())
    assert row["page_title"] == "Правила именования"
    assert row["source_url"] == "https://confl/viewpage?pageId=950276"
    assert row["anchor"] == "backup-pitr"
    assert row["page_id"] == "950276"
    assert row["heading_path"] == "Backup › PITR"
    assert row["space"] == "PAAS"


def test_both_collections_build_identical_shape() -> None:
    hit = _hit({"reader.page_title": "T"})
    assert KbDocCollection.row(hit).keys() == ConfluenceCollection.row(hit).keys()
    assert set(KbDocCollection.row(hit)) == _META_COLUMNS | _COLUMN_FIELDS


def test_meta_fields_reference_ingest_keys() -> None:
    # явное пересечение: META_FIELDS ссылается на те же MetadataKey, что ingest.
    conf = {f.column: f.key for f in ConfluenceCollection.META_FIELDS}
    doc = {f.column: f.key for f in KbDocCollection.META_FIELDS}
    # confluence-тип читает confluence-ключи
    assert conf["source_url"] is ConfluenceKeys.SOURCE_URL
    assert conf["page_id"] is ConfluenceKeys.PAGE_ID
    assert conf["space"] is ConfluenceKeys.SPACE_KEY
    # kbdoc-тип читает kbdoc-ключи (что пишет KbDocReader)
    assert doc["source_url"] is KbDocKeys.SOURCE_URL
    assert doc["page_id"] is KbDocKeys.PAGE_ID
    assert doc["space"] is KbDocKeys.SPACE
    # общие слои — одни и те же константы
    for shared in ("page_title", "anchor", "heading_path"):
        assert conf[shared] is doc[shared]
    assert conf["page_title"] is ReaderKeys.PAGE_TITLE
    assert conf["heading_path"] is SectionKeys.HEADING_PATH


def test_wire_keys_aligned_across_collections() -> None:
    # kbdoc выровнен под confluence: одинаковые wire-имена → строки совместимы.
    conf = {f.column: f.key.name for f in ConfluenceCollection.META_FIELDS}
    doc = {f.column: f.key.name for f in KbDocCollection.META_FIELDS}
    assert conf == doc


def test_missing_keys_become_empty() -> None:
    row = ConfluenceCollection.row(_hit({"reader.page_title": "T"}))
    assert row["page_title"] == "T"
    assert row["source_url"] == ""
    assert row["link"] == ""  # нет source_url → пустой link
    assert row["tags"] == ""
