"""Хост профиля: точное имя или шаблон *.domain; привязка к хосту URL."""

from __future__ import annotations

from boba.connections.http import HostPattern, HttpProfile


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
        profile = HttpProfile(
            base_url="https://*.example.com:8443/wiki", ssl_verify=False
        )
        if not profile.covers("wiki.example.com"):
            raise AssertionError("wildcard profile must cover the subdomain")
        if profile.covers("example.com"):
            raise AssertionError("foreign host must not be covered")

        bound = profile.bound_to("wiki.example.com")
        if bound.base_url != "https://wiki.example.com:8443/wiki":
            raise AssertionError(bound.base_url)

    def test_exact_profile_is_not_copied(self) -> None:
        profile = HttpProfile(base_url="https://wiki.example.com", ssl_verify=False)
        if profile.bound_to("x") is not profile:
            raise AssertionError("exact profile must not be copied")

    def test_profile_without_base_url_covers_nothing(self) -> None:
        if HttpProfile(ssl_verify=False).covers("any.example.com"):
            raise AssertionError("no base_url, no host")
