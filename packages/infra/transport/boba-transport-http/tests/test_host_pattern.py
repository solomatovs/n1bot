"""Хост профиля: точное имя или шаблон *.domain; привязка к хосту URL."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from boba.transport.http.connection import HostPattern, HttpConnection, UrlScheme


class TestHostPattern:
    def test_exact_host_matches_itself_only(self) -> None:
        pattern = HostPattern(value="wiki.example.com")
        if not pattern.matches("wiki.example.com"):
            raise AssertionError("exact host must match itself")
        if pattern.matches("deep.wiki.example.com"):
            raise AssertionError("exact host must not match a subdomain")

    def test_wildcard_covers_any_depth_but_not_the_apex(self) -> None:
        pattern = HostPattern(value="*.example.com")
        for host in ("ch01.example.com", "a.b.example.com"):
            if not pattern.matches(host):
                raise AssertionError(f"{host} must match *.example.com")
        if pattern.matches("example.com"):
            raise AssertionError("apex must not match its own wildcard")
        if pattern.matches("evil-example.com"):
            raise AssertionError("suffix must match on a label boundary")

    def test_matching_ignores_case_on_both_sides(self) -> None:
        pattern = HostPattern(value="*.Example.COM")
        if pattern.value != "*.example.com":
            raise AssertionError("config host must be lowercased")
        if not pattern.matches("Wiki.EXAMPLE.com"):
            raise AssertionError("mixed-case host must match")
        if not HostPattern(value="Wiki.Example.com").matches("wiki.example.com"):
            raise AssertionError("exact host must match regardless of case")

    def test_host_of_url_is_lowercase(self) -> None:
        host = HostPattern.host_of("https://Wiki.EXAMPLE.com:8443/x")
        if host != "wiki.example.com":
            raise AssertionError("host must be lowercased")
        if HostPattern.host_of("not a url") != "":
            raise AssertionError("no host must be empty")


class TestProfileBinding:
    def test_covers_and_bound_to_keep_scheme_port_and_path(self) -> None:
        connection = HttpConnection(
            host="*.example.com", port=8443, path="wiki/", ssl_verify=False
        )
        if not connection.covers("wiki.example.com"):
            raise AssertionError("wildcard connection must cover the subdomain")
        if connection.covers("example.com"):
            raise AssertionError("foreign host must not be covered")

        bound = connection.bound_to("wiki.example.com")
        if str(bound.root_url()) != "https://wiki.example.com:8443/wiki":
            raise AssertionError(str(bound.root_url()))

    def test_exact_profile_is_not_copied(self) -> None:
        connection = HttpConnection(host="wiki.example.com", port=443, ssl_verify=False)
        if connection.bound_to("x") is not connection:
            raise AssertionError("exact connection must not be copied")

    def test_host_is_lowercased_and_default_port_is_dropped(self) -> None:
        connection = HttpConnection(host="Wiki.EXAMPLE.com", port=443, ssl_verify=False)
        if connection.host != "wiki.example.com":
            raise AssertionError(connection.host)
        if str(connection.root_url()) != "https://wiki.example.com":
            raise AssertionError(str(connection.root_url()))

    def test_url_of_appends_the_path_under_path(self) -> None:
        connection = HttpConnection(
            scheme=UrlScheme.HTTP, host="h", port=8080, path="wiki", ssl_verify=False
        )
        got = str(connection.url_of("/rest/api/content?limit=1"))
        if got != "http://h:8080/wiki/rest/api/content?limit=1":
            raise AssertionError(got)

        root = HttpConnection(
            scheme=UrlScheme.HTTP, host="h", port=8080, ssl_verify=False
        )
        if str(root.url_of("rest/x")) != "http://h:8080/rest/x":
            raise AssertionError(str(root.url_of("rest/x")))


class TestAddressParts:
    def test_every_httpx_part_is_passed_through(self) -> None:
        connection = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="h",
            port=8080,
            path="/wiki",
            query="tenant=a",
            fragment="top",
            username="u",
            password=SecretStr("p:q"),
            ssl_verify=False,
        )
        if str(connection.root_url()) != "http://u:p%3Aq@h:8080/wiki?tenant=a#top":
            raise AssertionError(str(connection.root_url()))

    def test_raw_parts_override_the_split_ones(self) -> None:
        connection = HttpConnection(
            host="ignored",
            netloc="h:8443",
            userinfo=SecretStr("u:p"),
            raw_path="/a%20b?x=1",
            ssl_verify=False,
        )
        if str(connection.root_url()) != "https://u:p@h:8443/a%20b?x=1":
            raise AssertionError(str(connection.root_url()))
        if connection.address_host() != "h":
            raise AssertionError(connection.address_host())

    def test_public_url_and_trace_hide_credentials(self) -> None:
        connection = HttpConnection(
            host="h", port=443, username="u", password=SecretStr("secret")
        )
        if "secret" in str(connection.public_url()):
            raise AssertionError(str(connection.public_url()))
        if "secret" in connection.trace():
            raise AssertionError(connection.trace())

    def test_address_without_host_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs a host"):
            HttpConnection(path="/wiki")

    def test_url_of_keeps_credentials_and_drops_root_query(self) -> None:
        connection = HttpConnection(
            host="h",
            port=443,
            path="/wiki",
            query="tenant=a",
            userinfo=SecretStr("u:p"),
        )
        got = str(connection.url_of("/rest/api/content?limit=1"))
        if got != "https://u:p@h/wiki/rest/api/content?limit=1":
            raise AssertionError(got)

    def test_bound_wildcard_keeps_the_netloc_port(self) -> None:
        connection = HttpConnection(netloc="*.example.com:8443", ssl_verify=False)
        bound = connection.bound_to("wiki.example.com")
        if str(bound.root_url()) != "https://wiki.example.com:8443":
            raise AssertionError(str(bound.root_url()))
