"""ConfluenceJsonDecoder: REST-JSON → HTML-handle + обогащение metadata.

Integration: реальный `ConfluenceJsonDecoder` на реальном REST-payload'е
от Apache cwiki (см. `confluence_raw_doc` fixture). Раньше тут были
синтетические байты — теперь декодер тестируется на настоящем JSON'е,
который отдаёт `/rest/api/content/{id}?expand=body.view,version,space`.

Edge-case тесты (empty payload, missing body) удалены — на cwiki таких
ответов не бывает; pre-existing pure-data assert'ы были именно про
синтетику, которой больше нет.
"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import pytest

from boba.indexing import (
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
)
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpKeys

pytestmark = pytest.mark.integration


def test_decoder_id() -> None:
    """Decoder-id стабилен — используется DI/pipeline'ом для идентификации."""
    assert ConfluenceJsonDecoder().decoder_id() == DecoderId("ext.confluence_json")


def test_body_format_view_extracts_html(
    confluence_raw_doc: RawDocument,
) -> None:
    """Декодер вытаскивает HTML из body.view.value реальной страницы."""
    out = ConfluenceJsonDecoder(body_format="view").convert(confluence_raw_doc)
    html = out.handle.read()
    assert isinstance(html, bytes)
    assert len(html) > 0, "body.view.value пуст у реальной страницы — подозрительно"


def test_title_added_to_metadata(
    confluence_raw_doc: RawDocument,
) -> None:
    """`title` из JSON-ответа кладётся в `ReaderKeys.PAGE_TITLE`."""
    out = ConfluenceJsonDecoder(body_format="view").convert(confluence_raw_doc)
    title = out.metadata.get(ReaderKeys.PAGE_TITLE)
    assert title is not None
    assert len(title) > 0, "title пуст у реальной страницы"


def test_version_and_space_added_to_metadata(
    confluence_raw_doc: RawDocument,
) -> None:
    """`version.number` (int) и `space.key` (str) → ConfluenceKeys."""
    out = ConfluenceJsonDecoder(body_format="view").convert(confluence_raw_doc)
    version = out.metadata.get(ConfluenceKeys.VERSION)
    assert isinstance(version, int)
    assert version >= 1
    space_key = out.metadata.get(ConfluenceKeys.SPACE_KEY)
    assert space_key is not None
    assert len(space_key) > 0


def test_last_modified_added_from_version_when(
    confluence_raw_doc: RawDocument,
) -> None:
    """Если в metadata нет `last_modified` — Decoder берёт `version.when`."""
    # Убедимся, что upstream-metadata НЕ содержит last_modified.
    raw_no_lm = replace(
        confluence_raw_doc,
        handle=BytesIO(confluence_raw_doc.handle.read()),
        metadata=Metadata.empty(),
    )
    out = ConfluenceJsonDecoder(body_format="view").convert(raw_no_lm)
    last_modified = out.metadata.get(HttpKeys.LAST_MODIFIED)
    assert last_modified is not None
    assert "T" in last_modified, (
        "ISO-8601-timestamp ожидался от Confluence version.when, "
        f"got {last_modified!r}"
    )


def test_existing_last_modified_preserved(
    confluence_raw_doc: RawDocument,
) -> None:
    """Decoder не перезаписывает `last_modified`, если он уже выставлен upstream'ом."""
    upstream = "Wed, 01 Jan 2020 00:00:00 GMT"
    raw_with_lm = replace(
        confluence_raw_doc,
        handle=BytesIO(confluence_raw_doc.handle.read()),
        metadata=Metadata.empty().set(HttpKeys.LAST_MODIFIED, upstream),
    )
    out = ConfluenceJsonDecoder(body_format="view").convert(raw_with_lm)
    assert out.metadata.get(HttpKeys.LAST_MODIFIED) == upstream


def test_upstream_metadata_preserved(
    confluence_raw_doc: RawDocument,
) -> None:
    """Upstream `page_id`/`host` от RequestSource/Transport сохраняются."""
    # RawDocument из transport уже несёт ConfluenceKeys.PAGE_ID/HOST от
    # RequestSource. Проверяем, что декодер их не теряет.
    page_id_upstream = confluence_raw_doc.metadata.get(ConfluenceKeys.PAGE_ID)
    host_upstream = confluence_raw_doc.metadata.get(ConfluenceKeys.HOST)
    assert page_id_upstream is not None, "RequestSource обязан выставить PAGE_ID"
    assert host_upstream is not None, "RequestSource обязан выставить HOST"

    out = ConfluenceJsonDecoder(body_format="view").convert(confluence_raw_doc)
    assert out.metadata.get(ConfluenceKeys.PAGE_ID) == page_id_upstream
    assert out.metadata.get(ConfluenceKeys.HOST) == host_upstream


def test_source_id_preserved(
    confluence_raw_doc: RawDocument,
) -> None:
    """`source_id` — stable viewpage URL — не меняется при декодинге."""
    expected_source_id = confluence_raw_doc.source_id
    out = ConfluenceJsonDecoder(body_format="view").convert(confluence_raw_doc)
    assert out.source_id == expected_source_id
    assert "viewpage.action?pageId=" in str(out.source_id)
