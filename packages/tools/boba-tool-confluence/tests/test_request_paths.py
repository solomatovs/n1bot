"""Адреса Confluence REST: сегменты и query кодируются, а не склеиваются."""

from __future__ import annotations

from boba.tool.confluence.request_sources import ConfluenceRest


class TestConfluencePaths:
    def test_page_path_carries_the_expand(self) -> None:
        url = ConfluenceRest.page_fetch_path("123", body_format="export_view")

        assert url.path == "/rest/api/content/123"
        assert url.params["expand"].startswith("body.export_view,version,")

    def test_page_id_cannot_change_the_endpoint(self) -> None:
        url = ConfluenceRest.page_fetch_path("1?expand=x", body_format="view")

        assert url.path == "/rest/api/content/1?expand=x"
        assert str(url).startswith("/rest/api/content/1%3Fexpand%3Dx?expand=")

        traversal = ConfluenceRest.page_fetch_path("../space/FOO", body_format="view")
        assert traversal.path == "/rest/api/content/../space/FOO"
        assert "/rest/api/content/..%2Fspace%2FFOO?" in str(traversal)

    def test_attachments_path(self) -> None:
        url = ConfluenceRest.attachments_path("12/3", limit=10)

        assert url.path == "/rest/api/content/12/3/child/attachment"
        assert str(url) == (
            "/rest/api/content/12%2F3/child/attachment?limit=10&start=0&expand=version"
        )

    def test_page_body_path_has_no_attachments(self) -> None:
        url = ConfluenceRest.page_body_path("123", body_format="view")

        assert url.path == "/rest/api/content/123"
        assert url.params["expand"].startswith("body.view,version,")
        assert "children.attachment" not in url.params["expand"]

    def test_page_summary_path_lists_attachments_without_body(self) -> None:
        url = ConfluenceRest.page_summary_path("123")

        assert url.path == "/rest/api/content/123"
        assert "children.attachment" in url.params["expand"]
        assert "body." not in url.params["expand"]

    def test_space_content_path_walks_the_space(self) -> None:
        url = ConfluenceRest.space_content_path("A/B", limit=25)

        assert url.path == "/rest/api/space/A/B/content/page"
        assert str(url).startswith("/rest/api/space/A%2FB/content/page?limit=25")
        assert "children.attachment" in url.params["expand"]

    def test_space_list_path(self) -> None:
        assert str(ConfluenceRest.space_list_path("any")) == (
            "/rest/api/space?limit=50&start=0"
        )
        assert str(
            ConfluenceRest.space_list_path("global", expand="description.plain")
        ) == ("/rest/api/space?limit=50&start=0&type=global&expand=description.plain")

    def test_cql_search_path(self) -> None:
        url = ConfluenceRest.cql_search_path(
            'space = "DOC" and title ~ "a&b"', limit=5, start=10, expand="body.view"
        )

        assert url.path == "/rest/api/content/search"
        assert url.params["cql"] == 'space = "DOC" and title ~ "a&b"'
        assert url.params["limit"] == "5"
        assert url.params["start"] == "10"
        assert url.params["expand"] == "body.view"
