"""Пути Confluence REST: сегменты и query кодируются, а не склеиваются."""

from __future__ import annotations

from boba.tool.kb.confluence.request_sources import ConfluenceRest


class TestConfluencePaths:
    def test_page_path_keeps_expand_commas(self) -> None:
        path = ConfluenceRest.page_fetch_path("123", body_format="export_view")

        assert path.startswith("/rest/api/content/123?expand=body.export_view,version,")
        assert "%2C" not in path

    def test_page_id_cannot_change_the_endpoint(self) -> None:
        path = ConfluenceRest.page_fetch_path("1?expand=x", body_format="view")

        assert path.startswith("/rest/api/content/1%3Fexpand%3Dx?expand=")

        traversal = ConfluenceRest.page_fetch_path("../space/FOO", body_format="view")
        assert "/rest/api/content/..%2Fspace%2FFOO?" in traversal

    def test_space_pages_path(self) -> None:
        path = ConfluenceRest.space_pages_path("DOC/S", limit=10)

        assert path == "/rest/api/space/DOC%2FS/content?type=page&limit=10&start=0"

    def test_space_list_path(self) -> None:
        assert (
            ConfluenceRest.space_list_path("any") == "/rest/api/space?limit=50&start=0"
        )
        assert (
            ConfluenceRest.space_list_path("global", expand="description.plain")
            == "/rest/api/space?limit=50&start=0&type=global&expand=description.plain"
        )

    def test_cql_search_path(self) -> None:
        path = ConfluenceRest.cql_search_path(
            'space = "DOC" and title ~ "a&b"', limit=5, start=10, expand="body.view"
        )

        assert path == (
            "/rest/api/content/search?cql=space%20%3D%20%22DOC%22%20and%20title"
            "%20~%20%22a%26b%22&limit=5&start=10&expand=body.view"
        )
