"""WebConnection: whitelist (dict hostname→HttpConnection) + resolve → профиль."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.web.connection import WebConnection
from boba.transport.http import BearerAuth, HttpConnection, HttpxBearerAuth


def test_empty_whitelist_rejected() -> None:
    with pytest.raises(ValidationError, match="whitelist"):
        WebConnection()


def test_resolve_returns_profile() -> None:
    conn = WebConnection(profiles={"github.com": HttpConnection(timeout_sec=7.0)})
    p = conn.resolve_profile("https://github.com/x?q=1")
    assert isinstance(p, HttpConnection)
    assert p.timeout_sec == 7.0


def test_resolve_auth_from_profile() -> None:
    conn = WebConnection(
        profiles={
            "api.example.com": HttpConnection(
                auth=BearerAuth(method="bearer", token="tok"),
            ),
        },
    )
    p = conn.resolve_profile("https://api.example.com/path")
    assert isinstance(p.auth.httpx_auth(), HttpxBearerAuth)


def test_unknown_host_raises_with_allowlist() -> None:
    conn = WebConnection(profiles={"a.example.com": HttpConnection()})
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        conn.resolve_profile("https://evil.example.com/x")
    msg = str(exc_info.value)
    assert "evil.example.com" in msg
    assert "a.example.com" in msg


def test_case_insensitive_hostname() -> None:
    conn = WebConnection(profiles={"Docs.Python.ORG": HttpConnection()})
    assert isinstance(
        conn.resolve_profile("https://docs.python.org/3/"), HttpConnection
    )


def test_per_host_transport_params() -> None:
    conn = WebConnection(
        profiles={"x.example.com": HttpConnection(timeout_sec=7.0, ssl_verify=False)},
    )
    p = conn.resolve_profile("https://x.example.com/")
    assert p.timeout_sec == 7.0
    assert p.ssl_verify is False


def test_url_without_host_is_blocked() -> None:
    conn = WebConnection(profiles={"docs.python.org": HttpConnection()})
    with pytest.raises(ValueError, match="не в whitelist"):
        conn.resolve_profile("/relative/path")


def test_multiple_hosts_each_keeps_own_profile() -> None:
    conn = WebConnection(
        profiles={
            "pub.example.com": HttpConnection(),
            "api.example.com": HttpConnection(
                auth=BearerAuth(method="bearer", token="t"),
            ),
        },
    )
    assert conn.resolve_profile("https://pub.example.com/").auth.httpx_auth() is None
    assert isinstance(
        conn.resolve_profile("https://api.example.com/").auth.httpx_auth(),
        HttpxBearerAuth,
    )
