"""ConfluenceClient: REST-вызовы через httpx.MockTransport (без сети)."""

from __future__ import annotations

import httpx
import pytest

from boba.ext.confluence_source.auth import PatAuth
from boba.ext.confluence_source.client import ConfluenceClient


def _page_json(page_id: str, version: int = 1, body: str = "<p>x</p>") -> dict:
    return {
        "id": page_id,
        "title": f"Page {page_id}",
        "space": {"key": "DOCS"},
        "version": {"number": version, "when": "2024-01-01T00:00:00Z"},
        "body": {"export_view": {"value": body}},
        "ancestors": [{"title": "Root"}],
    }


def _build_client(handler) -> ConfluenceClient:
    """ConfluenceClient с подменённым httpx.MockTransport через ctor-DI."""
    return ConfluenceClient(
        base_url="https://confl.test",
        auth=PatAuth(token="t"),
        body_format="export_view",
        timeout_sec=10.0,
        transport=httpx.MockTransport(handler),
    )


def test_page_by_id_returns_parsed_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/content/12345"
        assert "expand" in request.url.params
        return httpx.Response(200, json=_page_json("12345", body="<h1>Hi</h1>"))

    with _build_client(handler) as client:
        page = client.page_by_id("12345")
    assert page.page_id == "12345"
    assert page.title == "Page 12345"
    assert page.space_key == "DOCS"
    assert page.version == 1
    assert page.body_html == "<h1>Hi</h1>"
    assert page.ancestors_path == "Root"


def test_pages_in_space_paginates():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={
                "results": [_page_json("1"), _page_json("2")],
                "_links": {"next": "/rest/api/space/DOCS/content?start=2"},
            })
        return httpx.Response(200, json={
            "results": [_page_json("3")],
            "_links": {},
        })

    with _build_client(handler) as client:
        pages = list(client.pages_in_space("DOCS"))
    assert [p.page_id for p in pages] == ["1", "2", "3"]
    assert calls["n"] == 2


def test_page_ids_in_space_extracts_ids_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": [{"id": "10"}, {"id": "20"}, {"id": "30"}],
            "_links": {},
        })

    with _build_client(handler) as client:
        ids = list(client.page_ids_in_space("DOCS"))
    assert ids == ["10", "20", "30"]


def test_pages_by_cql_passes_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url.path
        captured["cql"] = request.url.params.get("cql")
        return httpx.Response(200, json={
            "results": [_page_json("99")],
            "_links": {},
        })

    with _build_client(handler) as client:
        pages = list(client.pages_by_cql("space = DOCS"))
    assert captured["url"] == "/rest/api/content/search"
    assert captured["cql"] == "space = DOCS"
    assert pages[0].page_id == "99"


def test_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    with _build_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.page_by_id("1")


def test_page_from_json_handles_missing_fields():
    """REST-ответ с минимумом полей — без version/space/body — не падает."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "title": "T"})

    with _build_client(handler) as client:
        page = client.page_by_id("x")
    assert page.page_id == "x"
    assert page.title == "T"
    assert page.body_html == ""
    assert page.version == 0
    assert page.space_key == ""
    assert page.ancestors_path == ""


