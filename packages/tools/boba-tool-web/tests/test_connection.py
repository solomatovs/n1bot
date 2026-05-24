"""WebConnection: пустой whitelist → fail; resolve() корректно бьёт по host."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.web.auth import BasicAuth, BearerAuth, NoneAuth
from boba.tool.web.connection import WebConnection
from boba.tool.web.host_profile import WebHostProfile


def _conn(**hosts: WebHostProfile) -> WebConnection:
    return WebConnection(hosts=hosts)


def test_empty_hosts_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WebConnection(hosts={})
    assert "hosts" in str(exc_info.value).lower()


def test_resolve_known_host_returns_profile() -> None:
    profile = WebHostProfile(auth=BearerAuth(method="bearer", token="tok"))
    conn = _conn(**{"api.example.com": profile})
    got = conn.resolve("https://api.example.com/path?q=1")
    assert got is profile


def test_resolve_unknown_host_raises_with_allowlist() -> None:
    conn = _conn(**{"a.example.com": WebHostProfile(auth=NoneAuth(method="none"))})
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        conn.resolve("https://evil.example.com/x")
    msg = str(exc_info.value)
    assert "evil.example.com" in msg
    assert "a.example.com" in msg


def test_resolve_case_insensitive_host() -> None:
    """RFC 3986: hostname case-insensitive; whitelist хранит lowercase."""
    conn = _conn(**{"docs.python.org": WebHostProfile(auth=NoneAuth(method="none"))})
    profile = conn.resolve("https://Docs.Python.ORG/3/")
    assert isinstance(profile.auth, NoneAuth)


def test_make_transport_uses_connection_params() -> None:
    conn = _conn(
        **{"x.example.com": WebHostProfile(auth=NoneAuth(method="none"))},
    )
    conn = conn.model_copy(update={"timeout_sec": 7.0, "ssl_verify": False})
    tr = conn.make_transport()
    # Internal-поля _timeout/_verify — частная деталь HttpTransport'а, но проверка
    # что мы прокинули конкретные значения нужна. Доступ через name-mangled-имя.
    assert tr._timeout == 7.0
    assert tr._verify is False


def test_url_without_host_is_blocked() -> None:
    """Относительный URL без host → resolve должен фейлить (host='')."""
    conn = _conn(**{"docs.python.org": WebHostProfile(auth=NoneAuth(method="none"))})
    with pytest.raises(ValueError, match="не в whitelist"):
        conn.resolve("/relative/path")


def test_multiple_hosts_each_keeps_own_auth() -> None:
    a = WebHostProfile(auth=BasicAuth(method="basic", user="u", password="p"))
    b = WebHostProfile(auth=BearerAuth(method="bearer", token="tok"))
    conn = _conn(**{"a.example.com": a, "b.example.com": b})
    assert isinstance(conn.resolve("https://a.example.com/").auth, BasicAuth)
    assert isinstance(conn.resolve("https://b.example.com/").auth, BearerAuth)
