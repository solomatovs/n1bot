"""Discriminated WebAuth: pydantic-валидация + httpx_auth() контракт.

Покрытие:
- каждый вариант разбирается из dict с правильным `method`,
- обязательные поля (user/password/token) — без них ValidationError
  ссылается именно на нужный вариант (а не "any of");
- неизвестный method — ValidationError;
- extra-поля → ValidationError (model_config extra="forbid");
- httpx_auth() возвращает ожидаемый httpx.Auth-инстанс.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from boba.tool.web.auth import (
    BasicAuth,
    BearerAuth,
    DigestAuth,
    NoneAuth,
    WebAuth,
)
from boba.transport.http.auth import HttpxBearerAuth


class _AuthHolder(BaseModel):
    """Минимальный wrapper чтобы прогнать union через discriminator."""

    auth: WebAuth


def test_none_auth_round_trips() -> None:
    a = _AuthHolder(auth={"method": "none"}).auth  # type: ignore[arg-type]
    assert isinstance(a, NoneAuth)
    assert a.httpx_auth() is None


def test_basic_auth_round_trips() -> None:
    a = _AuthHolder(
        auth={"method": "basic", "user": "u", "password": "p"},  # type: ignore[arg-type]
    ).auth
    assert isinstance(a, BasicAuth)
    h = a.httpx_auth()
    assert isinstance(h, httpx.BasicAuth)


def test_bearer_auth_round_trips() -> None:
    a = _AuthHolder(
        auth={"method": "bearer", "token": "tok"},  # type: ignore[arg-type]
    ).auth
    assert isinstance(a, BearerAuth)
    h = a.httpx_auth()
    assert isinstance(h, HttpxBearerAuth)


def test_digest_auth_round_trips() -> None:
    a = _AuthHolder(
        auth={"method": "digest", "user": "u", "password": "p"},  # type: ignore[arg-type]
    ).auth
    assert isinstance(a, DigestAuth)
    h = a.httpx_auth()
    assert isinstance(h, httpx.DigestAuth)


def test_bearer_missing_token_fails_at_bearer_variant() -> None:
    """ValidationError должен явно ссылаться на BearerAuth, а не на 'any of'."""
    with pytest.raises(ValidationError) as exc_info:
        _AuthHolder(auth={"method": "bearer"})  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "token" in msg
    assert "bearer" in msg.lower()


def test_basic_missing_password_fails_at_basic_variant() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _AuthHolder(auth={"method": "basic", "user": "u"})  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "password" in msg


def test_unknown_method_fails() -> None:
    with pytest.raises(ValidationError):
        _AuthHolder(auth={"method": "oauth"})  # type: ignore[arg-type]


def test_extra_fields_rejected() -> None:
    """extra='forbid' — не даёт пронести лишний ключ незаметно."""
    with pytest.raises(ValidationError):
        TypeAdapter(WebAuth).validate_python(
            {"method": "none", "token": "leaked"},
        )


def test_basic_rejects_empty_strings() -> None:
    """min_length=1 — пустые user/password считаются ошибкой конфига."""
    with pytest.raises(ValidationError):
        _AuthHolder(
            auth={"method": "basic", "user": "", "password": "p"},  # type: ignore[arg-type]
        )
