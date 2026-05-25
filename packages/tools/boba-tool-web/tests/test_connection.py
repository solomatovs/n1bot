"""WebConnection: ACL + resolve по hostname + resolve_profiles из TOML."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.tool.web.auth import BasicAuth, BearerAuth, NoneAuth
from boba.tool.web.connection import WebConnection
from boba.tool.web.host_profile import WebHostProfile


def _profile(hostname: str, auth: object) -> WebHostProfile:
    return WebHostProfile(hostname=hostname, auth=auth)  # type: ignore[arg-type]


def _conn(**hosts: WebHostProfile) -> WebConnection:
    return WebConnection(hosts=hosts)


def test_empty_hosts_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WebConnection(hosts={})
    assert "профил" in str(exc_info.value).lower()


def test_resolve_known_host_returns_profile() -> None:
    profile = _profile("api.example.com", BearerAuth(method="bearer", token="tok"))
    conn = _conn(**{"api.example.com": profile})
    got = conn.resolve("https://api.example.com/path?q=1")
    assert got is profile


def test_resolve_unknown_host_raises_with_allowlist() -> None:
    conn = _conn(
        **{"a.example.com": _profile("a.example.com", NoneAuth(method="none"))},
    )
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        conn.resolve("https://evil.example.com/x")
    msg = str(exc_info.value)
    assert "evil.example.com" in msg
    assert "a.example.com" in msg


def test_resolve_case_insensitive_host() -> None:
    conn = _conn(
        **{
            "docs.python.org": _profile(
                "docs.python.org", NoneAuth(method="none"),
            ),
        },
    )
    profile = conn.resolve("https://Docs.Python.ORG/3/")
    assert isinstance(profile.auth, NoneAuth)


def test_make_transport_uses_connection_params() -> None:
    conn = _conn(
        **{"x.example.com": _profile("x.example.com", NoneAuth(method="none"))},
    )
    conn = conn.model_copy(update={"timeout_sec": 7.0, "ssl_verify": False})
    tr = conn.make_transport()
    assert tr._timeout == 7.0
    assert tr._verify is False


def test_url_without_host_is_blocked() -> None:
    conn = _conn(
        **{
            "docs.python.org": _profile(
                "docs.python.org", NoneAuth(method="none"),
            ),
        },
    )
    with pytest.raises(ValueError, match="не в whitelist"):
        conn.resolve("/relative/path")


def test_multiple_hosts_each_keeps_own_auth() -> None:
    a = _profile("a.example.com", BasicAuth(method="basic", user="u", password="p"))
    b = _profile("b.example.com", BearerAuth(method="bearer", token="tok"))
    conn = _conn(**{"a.example.com": a, "b.example.com": b})
    assert isinstance(conn.resolve("https://a.example.com/").auth, BasicAuth)
    assert isinstance(conn.resolve("https://b.example.com/").auth, BearerAuth)


def test_hostname_lowercased_in_profile() -> None:
    p = _profile("API.Example.COM", NoneAuth(method="none"))
    assert p.hostname == "api.example.com"


# --------------------------------------------------------------------------- #
# profiles → hosts через [web.<name>] секции TOML
# --------------------------------------------------------------------------- #


def _write_toml(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


def test_profiles_resolved_from_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [web.github_public]
        hostname = "github.com"
        [web.github_public.auth]
        method = "none"

        [web.confluence_pat]
        hostname = "confl.example.com"
        [web.confluence_pat.auth]
        method = "bearer"
        token = "TOK"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    conn = WebConnection(profiles=["github_public", "confluence_pat"])
    assert set(conn.hosts) == {"github.com", "confl.example.com"}
    assert isinstance(conn.resolve("https://github.com/x").auth, NoneAuth)
    assert isinstance(conn.resolve("https://confl.example.com/y").auth, BearerAuth)


def test_missing_profile_section_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(tmp_path, "")
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="не найдена или пуста"):
        WebConnection(profiles=["nonexistent"])


def test_profile_without_hostname_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [web.bad]
        [web.bad.auth]
        method = "none"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="отсутствует hostname"):
        WebConnection(profiles=["bad"])


def test_duplicate_hostname_across_profiles_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [web.a]
        hostname = "dup.example.com"
        [web.a.auth]
        method = "none"

        [web.b]
        hostname = "dup.example.com"
        [web.b.auth]
        method = "bearer"
        token = "T"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="нескольких профилях"):
        WebConnection(profiles=["a", "b"])
