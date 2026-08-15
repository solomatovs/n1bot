"HTTP-auth: HttpxBearerAuth (Bearer, которого нет в httpx) + WebAuth (union по method)"

from __future__ import annotations

from collections.abc import Generator, Mapping
from typing import Annotated, ClassVar, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
)

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "HttpxBearerAuth",
    "NoneAuth",
    "WebAuth",
]


class HttpxBearerAuth(httpx.Auth):
    "Authorization: Bearer <token> для httpx; header-mutation без refresh-flow"

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class _AuthBase(BaseModel):
    """Общая база: запрет лишних полей + контракт httpx_auth()."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_SECRETS: ClassVar[str] = "reveal_secrets"
    """Ключ контекста сериализации: секрет раскрывается только с ним."""

    def httpx_auth(self) -> httpx.Auth | None:
        raise NotImplementedError

    @classmethod
    def _reveal(cls, value: SecretStr, info: SerializationInfo) -> str | None:
        """Секрет уходит в дамп только с REVEAL_SECRETS в контексте."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(cls.REVEAL_SECRETS):
            return None

        return value.get_secret_value()


class NoneAuth(_AuthBase):
    """Anonymous-доступ. method='none' обязан быть прописан явно."""

    method: Literal["none"]

    def httpx_auth(self) -> None:
        return None


class BasicAuth(_AuthBase):
    """HTTP Basic: httpx.BasicAuth(user, password)."""

    method: Literal["basic"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return httpx.BasicAuth(self.user, self.password.get_secret_value())

    @field_serializer("password", when_used="json")
    def _dump_password(
        self, value: SecretStr, info: SerializationInfo
    ) -> str | None:
        return self._reveal(value, info)


class BearerAuth(_AuthBase):
    """Authorization: Bearer <token> через HttpxBearerAuth."""

    method: Literal["bearer"]
    token: SecretStr = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return HttpxBearerAuth(self.token.get_secret_value())

    @field_serializer("token", when_used="json")
    def _dump_token(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class DigestAuth(_AuthBase):
    """HTTP Digest: httpx.DigestAuth(user, password)."""

    method: Literal["digest"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    def httpx_auth(self) -> httpx.Auth:
        return httpx.DigestAuth(self.user, self.password.get_secret_value())

    @field_serializer("password", when_used="json")
    def _dump_password(
        self, value: SecretStr, info: SerializationInfo
    ) -> str | None:
        return self._reveal(value, info)


WebAuth = Annotated[
    NoneAuth | BasicAuth | BearerAuth | DigestAuth,
    Field(discriminator="method"),
]
"""Discriminated union по method — точная диагностика ошибок валидации."""
