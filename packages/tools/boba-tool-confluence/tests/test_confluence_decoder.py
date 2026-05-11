"""ConfluenceJsonDecoder: REST-JSON → HTML-handle + обогащение metadata."""

from __future__ import annotations

import json
from io import BytesIO

from boba.indexing import (
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
    SourceId,
)
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpKeys


def _doc(payload: bytes, *, metadata: Metadata | None = None) -> RawDocument:
    return RawDocument(
        handle=BytesIO(payload),
        source_id=SourceId(
            "https://confl.test/pages/viewpage.action?pageId=1"
        ),
        metadata=metadata or Metadata.empty(),
    )


def test_decoder_id():
    assert ConfluenceJsonDecoder().decoder_id() == DecoderId("ext.confluence_json")


def test_default_body_format_export_view():
    payload = json.dumps(
        {
            "title": "Page Title",
            "body": {"export_view": {"value": "<p>hello</p>"}},
        }
    ).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_doc(payload))
    assert out.handle.read() == b"<p>hello</p>"
    assert out.metadata.get(ReaderKeys.PAGE_TITLE) == "Page Title"


def test_custom_body_format():
    payload = json.dumps(
        {
            "title": "T",
            "body": {"storage": {"value": "<storage/>"}},
        }
    ).encode("utf-8")
    out = ConfluenceJsonDecoder(body_format="storage").convert(_doc(payload))
    assert out.handle.read() == b"<storage/>"


def test_version_added_to_metadata():
    payload = json.dumps(
        {
            "title": "T",
            "body": {"export_view": {"value": "<p>x</p>"}},
            "version": {"number": 7, "when": "2024-01-15T10:00:00Z"},
        }
    ).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_doc(payload))
    assert out.metadata.get(ConfluenceKeys.VERSION) == 7
    assert out.metadata.get(HttpKeys.LAST_MODIFIED) == "2024-01-15T10:00:00Z"


def test_existing_last_modified_preserved():
    """Если HttpTransport уже положил last_modified — Decoder его не перезаписывает."""
    payload = json.dumps(
        {
            "title": "T",
            "body": {"export_view": {"value": "<p/>"}},
            "version": {"when": "2024-02-01"},
        }
    ).encode("utf-8")
    upstream = "Wed, 01 Jan 2020 00:00:00 GMT"
    upstream_md = Metadata.empty().set(HttpKeys.LAST_MODIFIED, upstream)
    out = ConfluenceJsonDecoder().convert(
        _doc(payload, metadata=upstream_md),
    )
    assert out.metadata.get(HttpKeys.LAST_MODIFIED) == upstream


def test_upstream_metadata_preserved():
    """confluence_page_id, etag и т.п. от RequestSource/Transport сохраняются."""
    payload = json.dumps(
        {
            "title": "T",
            "body": {"export_view": {"value": "<p/>"}},
        }
    ).encode("utf-8")
    upstream = (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, "777")
        .set(ConfluenceKeys.HOST, "confl.test")
    )
    out = ConfluenceJsonDecoder().convert(
        _doc(payload, metadata=upstream),
    )
    assert out.metadata.get(ConfluenceKeys.PAGE_ID) == "777"
    assert out.metadata.get(ConfluenceKeys.HOST) == "confl.test"


def test_empty_payload_passthrough():
    """Пустой ответ — без падения, document возвращается как есть."""
    doc = _doc(b"")
    out = ConfluenceJsonDecoder().convert(doc)
    assert out is doc


def test_missing_body_yields_empty_html():
    payload = json.dumps({"title": "T"}).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_doc(payload))
    assert out.handle.read() == b""
    assert out.metadata.get(ReaderKeys.PAGE_TITLE) == "T"


def test_source_id_preserved():
    payload = json.dumps(
        {
            "title": "T",
            "body": {"export_view": {"value": "<p/>"}},
        }
    ).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_doc(payload))
    assert (
        out.source_id.to_wire()
        == "https://confl.test/pages/viewpage.action?pageId=1"
    )
