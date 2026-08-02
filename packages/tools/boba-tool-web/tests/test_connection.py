"""WebConnection: whitelist (dict hostname->HttpConnection) + resolve -> профиль."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from boba.tool.web.connection import WebConnection
from boba.transport.http import BearerAuth, HttpProfile, HttpxBearerAuth


def test_empty_whitelist_rejected() -> None:
    with pytest.raises(ValidationError, match="whitelist"):
        WebConnection()


def test_resolve_returns_profile() -> None:
    conn = WebConnection(profiles={"github.com": HttpProfile(timeout_sec=7.0)})
    p = conn.resolve_profile("https://github.com/x?q=1")
    assert isinstance(p, HttpProfile)
    assert p.timeout_sec == 7.0


def test_resolve_auth_from_profile() -> None:
    conn = WebConnection(
        profiles={
            "api.example.com": HttpProfile(
                auth=BearerAuth(method="bearer", token=SecretStr("tok")),
            ),
        },
    )
    p = conn.resolve_profile("https://api.example.com/path")
    assert isinstance(p.auth.httpx_auth(), HttpxBearerAuth)


def test_unknown_host_raises_with_allowlist() -> None:
    conn = WebConnection(profiles={"a.example.com": HttpProfile()})
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        conn.resolve_profile("https://evil.example.com/x")
    msg = str(exc_info.value)
    assert "evil.example.com" in msg
    assert "a.example.com" in msg


def test_case_insensitive_hostname() -> None:
    conn = WebConnection(profiles={"Docs.Python.ORG": HttpProfile()})
    assert isinstance(conn.resolve_profile("https://docs.python.org/3/"), HttpProfile)


def test_per_host_transport_params() -> None:
    conn = WebConnection(
        profiles={"x.example.com": HttpProfile(timeout_sec=7.0, ssl_verify=False)},
    )
    p = conn.resolve_profile("https://x.example.com/")
    assert p.timeout_sec == 7.0
    assert p.ssl_verify is False


def test_url_without_host_is_blocked() -> None:
    conn = WebConnection(profiles={"docs.python.org": HttpProfile()})
    with pytest.raises(ValueError, match="не в whitelist"):
        conn.resolve_profile("/relative/path")


def test_multiple_hosts_each_keeps_own_profile() -> None:
    conn = WebConnection(
        profiles={
            "pub.example.com": HttpProfile(),
            "api.example.com": HttpProfile(
                auth=BearerAuth(method="bearer", token=SecretStr("t")),
            ),
        },
    )
    assert conn.resolve_profile("https://pub.example.com/").auth.httpx_auth() is None
    assert isinstance(
        conn.resolve_profile("https://api.example.com/").auth.httpx_auth(),
        HttpxBearerAuth,
    )
