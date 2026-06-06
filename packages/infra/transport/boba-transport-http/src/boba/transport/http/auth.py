"""HTTP-auth: `HttpxBearerAuth` + `WebAuth` (discriminated union по `method`).

httpx даёт из коробки `BasicAuth`/`DigestAuth`/`NetRCAuth`, но не имеет
встроенного Bearer/PAT — `HttpxBearerAuth` закрывает это.

`WebAuth` — конфиг-уровневый discriminated union auth-методов; каждый вариант
реализует `httpx_auth() -> httpx.Auth | None`, результат идёт прямо в
`HttpRequest.auth` → `httpx.Client(auth=...)`. Живёт здесь (инфра-слой HTTP),
чтобы переиспользоваться разными consumer'ами (web, confluence, …).
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "HttpxBearerAuth",
    "NoneAuth",
    "WebAuth",
]


class HttpxBearerAuth(httpx.Auth):
    """`Authorization: Bearer <token>` для httpx.

    Используется любыми API, требующими Bearer-токен (Atlassian PAT,
    GitHub PAT, generic OAuth2 access token). Pure header-mutation,
    без refresh-flow.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


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
