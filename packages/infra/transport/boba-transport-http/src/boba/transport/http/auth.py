"""HTTP-auth: Bearer и Negotiate, которых нет в httpx, плюс WebAuth (union по method).

Ошибки:
KerberosError — у negotiate-профиля не выпущен SPNEGO-токен; идёт из
    httpx-потока запроса наружу как есть.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator, Mapping
from typing import Annotated, Any, ClassVar, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
)

from boba.krb import (
    ClientCredentials,
    Kerberos,
    KerberosCredentials,
    KerberosDump,
    KerberosError,
    SpnegoNegotiate,
)

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "HttpxBearerAuth",
    "HttpxNegotiateAuth",
    "NegotiateAuth",
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


class HttpxNegotiateAuth(httpx.Auth):
    """Authorization: Negotiate по кредам kerberos; токен свой на каждый запрос.

    Токен строится по окружению процесса, поэтому запрос идёт внутри
    applied()/applied_async() кредов — лок KerberosEnv держится только на
    время выпуска токена. Сервис с отдельным login-сервлетом (Confluence
    Kerberos SSO) аутентифицирует только его: тогда первым идёт запрос
    на login_url, а полученная сессионная cookie едет в каждый запрос.
    """

    def __init__(
        self,
        credentials: KerberosCredentials,
        service: str,
        login_url: str | None,
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._login_url = login_url
        self._session = httpx.Cookies()

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        with self._credentials.applied():
            header = self._header()

        if self._login_url is not None and not self._session:
            response = yield self._login_request(header)
            self._remember(response)

        self._session.set_cookie_header(request)
        request.headers[SpnegoNegotiate.HEADER] = header
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        async with self._credentials.applied_async():
            header = self._header()

        if self._login_url is not None and not self._session:
            response = yield self._login_request(header)
            self._remember(response)

        self._session.set_cookie_header(request)
        request.headers[SpnegoNegotiate.HEADER] = header
        yield request

    def _header(self) -> str:
        return SpnegoNegotiate.header(self._service)

    def _login_request(self, header: str) -> httpx.Request:
        return httpx.Request(
            "GET", str(self._login_url), headers={SpnegoNegotiate.HEADER: header}
        )

    def _remember(self, response: httpx.Response) -> None:
        """Сессия login-сервлета; 401 — Negotiate там не приняли.

        Cookie сессии сервлет ставит на редиректе, а клиент с
        follow_redirects его уже прошёл — поэтому смотрим всю историю.
        """
        if response.status_code == httpx.codes.UNAUTHORIZED:
            msg = f"negotiate login at {self._login_url} was refused"
            raise KerberosError(msg)

        for hop in (*response.history, response):
            self._session.extract_cookies(hop)

        if not self._session:
            msg = f"negotiate login at {self._login_url} set no session cookie"
            raise KerberosError(msg)


class _AuthBase(BaseModel):
    """Общая база: запрет лишних полей + контракт httpx_auth(service)."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"
    """Ключ обязан совпадать с SecretRevealing.REVEAL_CONTEXT из toolkit."""

    def httpx_auth(self, service: str) -> httpx.Auth | None:
        """Аутентификатор httpx; service (HTTP@host) нужен только negotiate."""
        raise NotImplementedError

    @classmethod
    def _reveal(cls, value: SecretStr, info: SerializationInfo) -> str | None:
        """Секрет уходит в дамп только с REVEAL_CONTEXT в контексте."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(cls.REVEAL_CONTEXT):
            return None

        return value.get_secret_value()


class NoneAuth(_AuthBase):
    """Anonymous-доступ. method='none' обязан быть прописан явно."""

    method: Literal["none"]

    def httpx_auth(self, service: str) -> None:
        return None


class BasicAuth(_AuthBase):
    """HTTP Basic: httpx.BasicAuth(user, password)."""

    method: Literal["basic"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    def httpx_auth(self, service: str) -> httpx.Auth:
        return httpx.BasicAuth(self.user, self.password.get_secret_value())

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class BearerAuth(_AuthBase):
    """Authorization: Bearer <token> через HttpxBearerAuth."""

    method: Literal["bearer"]
    token: SecretStr = Field(min_length=1)

    def httpx_auth(self, service: str) -> httpx.Auth:
        return HttpxBearerAuth(self.token.get_secret_value())

    @field_serializer("token", when_used="json")
    def _dump_token(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class DigestAuth(_AuthBase):
    """HTTP Digest: httpx.DigestAuth(user, password)."""

    method: Literal["digest"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    def httpx_auth(self, service: str) -> httpx.Auth:
        return httpx.DigestAuth(self.user, self.password.get_secret_value())

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class NegotiateAuth(_AuthBase):
    """Kerberos/SPNEGO: Authorization: Negotiate по kerberos-секции профиля.

    В конфиге — keytab сервиса либо delegated (идёт сам пользователь);
    в песочницу уезжает билет одного вызова к HTTP@host профиля.
    """

    method: Literal["negotiate"]
    kerberos: Kerberos = Field(
        description="Креды: keytab, delegated или билет вызова.",
    )
    service_host: str | None = Field(
        default=None,
        description=(
            "Хост SPN (HTTP/<service_host>), если он отличается от хоста "
            "base_url: адрес по IP, reverse proxy. None — хост base_url."
        ),
    )
    login_path: str | None = Field(
        default=None,
        description=(
            "Путь login-сервлета, если сервис принимает Negotiate только там "
            "(Confluence Kerberos SSO: /plugins/servlet/kerberos/ntlm/login); "
            "сессионная cookie оттуда едет в остальные запросы. None — Negotiate "
            "на каждом запросе."
        ),
    )

    def httpx_auth(self, service: str) -> httpx.Auth:
        return self.httpx_auth_at(service, None)

    def httpx_auth_at(self, service: str, login_url: str | None) -> httpx.Auth:
        return HttpxNegotiateAuth(
            ClientCredentials.of(self.kerberos), service, login_url
        )

    @field_serializer("kerberos", when_used="json")
    def _dump_kerberos(
        self, value: Kerberos, info: SerializationInfo
    ) -> dict[str, Any] | None:
        return KerberosDump.json(value, info.context, "web connection")


WebAuth = Annotated[
    NoneAuth | BasicAuth | BearerAuth | DigestAuth | NegotiateAuth,
    Field(discriminator="method"),
]
"""Discriminated union по method — точная диагностика ошибок валидации."""
