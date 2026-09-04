"""HTTP-auth для httpx: Bearer и Negotiate, которых нет в httpx, и фабрика
аутентификатора по профилю соединения (модели — boba.transport.http.profile).

Ошибки:
KerberosError — у negotiate-профиля не выпущен SPNEGO-токен; идёт из
    httpx-потока запроса наружу как есть.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import httpx

from boba.kerberos import KerberosError
from boba.krb import ClientCredentials, KerberosCredentials, SpnegoNegotiate
from boba.transport.http.profile import (
    BasicAuth,
    BearerAuth,
    DigestAuth,
    HttpConnection,
    NegotiateAuth,
    WebAuth,
)

__all__ = [
    "HttpxAuth",
    "HttpxBearerAuth",
    "HttpxNegotiateAuth",
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


class HttpxAuth:
    """Аутентификатор httpx по профилю: negotiate получает SPN и login-URL."""

    @classmethod
    def of(cls, profile: HttpConnection) -> httpx.Auth | None:
        login_url = profile.login_url()
        if isinstance(profile.auth, NegotiateAuth):
            return cls.of_auth(profile.auth, profile.service_name(), login_url)

        return cls.of_auth(profile.auth, "", login_url)

    @staticmethod
    def of_auth(
        auth: WebAuth, service: str, login_url: str | None
    ) -> httpx.Auth | None:
        if isinstance(auth, BasicAuth):
            return httpx.BasicAuth(auth.user, auth.password.get_secret_value())

        if isinstance(auth, DigestAuth):
            return httpx.DigestAuth(auth.user, auth.password.get_secret_value())

        if isinstance(auth, BearerAuth):
            return HttpxBearerAuth(auth.token.get_secret_value())

        if isinstance(auth, NegotiateAuth):
            return HttpxNegotiateAuth(
                ClientCredentials.of(auth.kerberos), service, login_url
            )

        return None
