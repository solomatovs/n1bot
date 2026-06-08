"""Online CQL-поиск: version.number хита -> metadata + колонка `version`.

LLM сверяет online-version (отсюда) с version из kb-поиска по индексу, чтобы
понять, что в индексе устарело и страницу нужно переиндексировать.
"""

from __future__ import annotations

import json
from io import BytesIO

from boba.indexing import Metadata, RawDocument, SourceId
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.confluence.reading import ConfluenceSearchHitsReader
from boba.tool.kb.confluence.search_cql import CqlSearch

_SEARCH_RESPONSE = {
    "results": [
        {
            "id": "983136",
            "title": "Backup",
            "version": {"number": 7, "when": "2026-03-18T19:00:00Z"},
            "space": {"key": "PAAS"},
            "body": {"view": {"value": "<p>hi</p>"}},
            "_links": {"webui": "/pages/viewpage.action?pageId=983136"},
        },
    ],
    "_links": {"base": "https://confl.loshara.com"},
}


def _read_one():
    raw = RawDocument(
        handle=BytesIO(json.dumps(_SEARCH_RESPONSE).encode("utf-8")),
        source_id=SourceId("confluence:cql"),
        metadata=Metadata.empty(),
    )
    reader = ConfluenceSearchHitsReader(
        base_url="https://confl.loshara.com", snippet_chars=300,
    )
    return next(iter(reader.read(raw)))


def test_online_hit_carries_version() -> None:
    section = _read_one()
    assert section.metadata.get(ConfluenceKeys.VERSION) == 7


def test_cql_hit_surfaces_version_column() -> None:
    row = CqlSearch.hit(_read_one())
    assert row["version"] == "7"
    assert row["url"] == "https://confl.loshara.com/pages/viewpage.action?pageId=983136"
