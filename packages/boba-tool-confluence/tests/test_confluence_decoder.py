"""ConfluenceJsonDecoder: REST-JSON → HTML-handle + обогащение metadata."""

from __future__ import annotations

import json
from io import BytesIO

from boba.ext.confluence_tools.decoder import ConfluenceJsonDecoder
from boba.processing import DecoderId, IndexingContext, PipelineId, RawDocument


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _doc(payload: bytes, *, metadata: dict[str, str] | None = None) -> RawDocument:
    return RawDocument(
        handle=BytesIO(payload),
        source_id="https://confl.test/pages/viewpage.action?pageId=1",
        content_hint="application/json",
        metadata=metadata or {},
    )


def test_decoder_id():
    assert ConfluenceJsonDecoder().decoder_id() == DecoderId("ext.confluence_json")


def test_default_body_format_export_view():
    payload = json.dumps({
        "title": "Page Title",
        "body": {"export_view": {"value": "<p>hello</p>"}},
    }).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_ctx(), _doc(payload))
    assert out.handle.read() == b"<p>hello</p>"
    assert out.content_hint == "confluence_html"
    assert out.metadata["title"] == "Page Title"


def test_custom_body_format():
    payload = json.dumps({
        "title": "T",
        "body": {"storage": {"value": "<storage/>"}},
    }).encode("utf-8")
    out = ConfluenceJsonDecoder(body_format="storage").convert(_ctx(), _doc(payload))
    assert out.handle.read() == b"<storage/>"


def test_version_added_to_metadata():
    payload = json.dumps({
        "title": "T",
        "body": {"export_view": {"value": "<p>x</p>"}},
        "version": {"number": 7, "when": "2024-01-15T10:00:00Z"},
    }).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_ctx(), _doc(payload))
    assert out.metadata["version"] == "7"
    assert out.metadata["last_modified"] == "2024-01-15T10:00:00Z"


def test_existing_last_modified_preserved():
    """Если HttpTransport уже положил last_modified — Decoder его не перезаписывает."""
    payload = json.dumps({
        "title": "T",
        "body": {"export_view": {"value": "<p/>"}},
        "version": {"when": "2024-02-01"},
    }).encode("utf-8")
    upstream = "Wed, 01 Jan 2020 00:00:00 GMT"
    out = ConfluenceJsonDecoder().convert(
        _ctx(), _doc(payload, metadata={"last_modified": upstream}),
    )
    assert out.metadata["last_modified"] == upstream


def test_upstream_metadata_preserved():
    """confluence_page_id, etag и т.п. от RequestSource/Transport сохраняются."""
    payload = json.dumps({
        "title": "T",
        "body": {"export_view": {"value": "<p/>"}},
    }).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(
        _ctx(),
        _doc(payload, metadata={"confluence_page_id": "777", "etag": "abc"}),
    )
    assert out.metadata["confluence_page_id"] == "777"
    assert out.metadata["etag"] == "abc"


def test_empty_payload_passthrough():
    """Пустой ответ — без падения, document возвращается как есть."""
    doc = _doc(b"")
    out = ConfluenceJsonDecoder().convert(_ctx(), doc)
    assert out is doc


def test_missing_body_yields_empty_html():
    payload = json.dumps({"title": "T"}).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_ctx(), _doc(payload))
    assert out.handle.read() == b""
    assert out.metadata["title"] == "T"


def test_source_id_preserved():
    payload = json.dumps({
        "title": "T",
        "body": {"export_view": {"value": "<p/>"}},
    }).encode("utf-8")
    out = ConfluenceJsonDecoder().convert(_ctx(), _doc(payload))
    assert out.source_id == "https://confl.test/pages/viewpage.action?pageId=1"
