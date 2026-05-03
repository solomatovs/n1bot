"""Auth: PAT → Bearer header; Basic → httpx.BasicAuth; ошибки конфигурации."""

from __future__ import annotations

import httpx
import pytest

from boba.ext.confluence_source.auth import (
    AuthError,
    BasicAuth,
    PatAuth,
)
from boba.ext.confluence_source.config import (
    ConfluenceCommonConfig,
    build_auth,
)


def test_pat_apply_sets_bearer_header():
    kw: dict = {}
    PatAuth(token="abc").apply(kw)
    assert kw["headers"]["Authorization"] == "Bearer abc"


def test_basic_apply_sets_httpx_basicauth():
    kw: dict = {}
    BasicAuth(user="alice", password="pw").apply(kw)
    assert isinstance(kw["auth"], httpx.BasicAuth)


def test_build_auth_pat():
    auth = build_auth(
        ConfluenceCommonConfig(auth_method="pat", auth_token="t"),
    )
    assert isinstance(auth, PatAuth)


def test_build_auth_basic():
    auth = build_auth(
        ConfluenceCommonConfig(
            auth_method="basic", auth_user="u", auth_token="p"
        ),
    )
    assert isinstance(auth, BasicAuth)


def test_build_auth_basic_requires_user():
    with pytest.raises(AuthError):
        build_auth(
            ConfluenceCommonConfig(auth_method="basic", auth_token="p"),
        )


def test_build_auth_empty_token():
    with pytest.raises(AuthError):
        build_auth(ConfluenceCommonConfig(auth_method="pat", auth_token=""))


def test_build_auth_unknown_method():
    with pytest.raises(AuthError):
        build_auth(
            ConfluenceCommonConfig(auth_method="oauth", auth_token="t"),
        )
