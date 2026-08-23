"""Хост профиля: точное имя или шаблон *.domain; привязка к хосту URL."""

from __future__ import annotations

from boba.transport.http import HostPattern, HttpProfile


class TestHostPattern:
    def test_exact_host_matches_itself_only(self) -> None:
        pattern = HostPattern(value="confl.loshara.com")
        if not pattern.matches("confl.loshara.com"):
            raise AssertionError("exact host must match itself")
        if pattern.matches("wiki.confl.loshara.com"):
            raise AssertionError("exact host must not match a subdomain")

    def test_wildcard_covers_any_depth_but_not_the_apex(self) -> None:
        pattern = HostPattern(value="*.loshara.com")
        for host in ("ch01.loshara.com", "a.b.loshara.com"):
            if not pattern.matches(host):
                raise AssertionError(f"{host} must match *.loshara.com")
        if pattern.matches("loshara.com"):
            raise AssertionError("apex must not match its own wildcard")
        if pattern.matches("evil-loshara.com"):
            raise AssertionError("suffix must match on a label boundary")

    def test_host_of_url_is_lowercase(self) -> None:
        host = HostPattern.host_of("https://Confl.LOSHARA.com:8443/x")
        if host != "confl.loshara.com":
            raise AssertionError("host must be lowercased")
        if HostPattern.host_of("not a url") != "":
            raise AssertionError("no host must be empty")


class TestProfileBinding:
    def test_covers_and_bound_to_keep_scheme_port_and_path(self) -> None:
        profile = HttpProfile(
            base_url="https://*.loshara.com:8443/wiki", ssl_verify=False
        )
        if not profile.covers("confl.loshara.com"):
            raise AssertionError("wildcard profile must cover the subdomain")
        if profile.covers("example.com"):
            raise AssertionError("foreign host must not be covered")

        bound = profile.bound_to("confl.loshara.com")
        if bound.base_url != "https://confl.loshara.com:8443/wiki":
            raise AssertionError(bound.base_url)

    def test_exact_profile_is_not_copied(self) -> None:
        profile = HttpProfile(base_url="https://confl.loshara.com", ssl_verify=False)
        if profile.bound_to("x") is not profile:
            raise AssertionError("exact profile must not be copied")

    def test_profile_without_base_url_covers_nothing(self) -> None:
        if HttpProfile(ssl_verify=False).covers("any.loshara.com"):
            raise AssertionError("no base_url, no host")
