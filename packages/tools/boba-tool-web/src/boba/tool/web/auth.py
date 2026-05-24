"""Web auth: discriminated union по `method` для per-host профилей.

Каждый поддерживаемый auth-метод — отдельный класс-вариант с собственными
обязательными полями. Pydantic при невалидном TOML укажет именно тот
вариант, который не подошёл (а не размытое `any of`).

Все варианты реализуют `httpx_auth() -> httpx.Auth | None` — итог идёт
прямо в `HttpRequest.auth` → `httpx.Client(auth=...)`. Transport ничего
про auth-схемы не знает.

Реализации:
- `NoneAuth`   → `None` (anonymous, обязан быть указан явно).
- `BasicAuth`  → `httpx.BasicAuth` (built-in).
- `BearerAuth` → `HttpxBearerAuth` (из `boba.transport.http.auth`).
- `DigestAuth` → `httpx.DigestAuth` (built-in).
"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.transport.http.auth import HttpxBearerAuth

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "NoneAuth",
    "WebAuth",
]


class _AuthBase(BaseModel):
    """Общая база: запрет лишних полей + контракт `httpx_auth()`."""

    model_config = ConfigDict(extra="forbid")

    def httpx_auth(self) -> httpx.Auth | None:
        raise NotImplementedError


class NoneAuth(_AuthBase):
    """Anonymous-доступ. `method='none'` обязан быть прописан явно."""

    method: Literal["none"]

    def httpx_auth(self) -> None:
        return None


class BasicAuth(_AuthBase):
    """HTTP Basic: `httpx.BasicAuth(user, password)`."""

    method: Literal["basic"]
    user: str = Field(min_length=1)
    password: str = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return httpx.BasicAuth(self.user, self.password)


class BearerAuth(_AuthBase):
    """`Authorization: Bearer <token>` через `HttpxBearerAuth`."""

    method: Literal["bearer"]
    token: str = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return HttpxBearerAuth(self.token)


class DigestAuth(_AuthBase):
    """HTTP Digest: `httpx.DigestAuth(user, password)`."""

    method: Literal["digest"]
    user: str = Field(min_length=1)
    password: str = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return httpx.DigestAuth(self.user, self.password)


WebAuth = Annotated[
    NoneAuth | BasicAuth | BearerAuth | DigestAuth,
    Field(discriminator="method"),
]
"""Discriminated union по `method` — точная диагностика ошибок валидации."""
