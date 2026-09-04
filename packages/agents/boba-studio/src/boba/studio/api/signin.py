"""Вход через api: пароль и SPNEGO сервисом входа, JWT и cookie.

SSO: GET /auth/sso?next= — обмен на своём URL, после входа 303 на страницу;
POST /auth/sso/refresh — свежий билет для живой сессии.

Ошибки: свои не выпускает — ошибки сервиса входа (BaseError) переводит в HTTP
DomainErrorMiddleware; AuthorizationError — вход/выход без метки своего запроса
(OwnRequest); роуты SSO монтируются только при настроенном SSO.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from boba.auth import AuthService, IssuedSession
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.sso import SsoChallenge, SsoErrorCode, SsoRefused
from boba.runtime.http import SessionCookie, SsoRequests, SsoResponses
from boba.studio.api.urls import SignInUrl

__all__ = [
    "Credentials",
    "PageUrls",
    "SignInApi",
    "SignInProviders",
    "SignInWiring",
]


@dataclass(frozen=True)
class PageUrls:
    """Адреса страницы для исходов SSO: корень для next, логин для отказов."""

    root: str
    login: str
    home: str


@dataclass(frozen=True)
class SignInWiring:
    """Что нужно входу: сервис входа, адрес SSO и адреса страницы."""

    auth: AuthService
    sso_url: str
    page: PageUrls


class SignInProviders(BaseModel):
    """Что доступно форме: пароль и/или адрес SSO (пусто — нет)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: bool
    sso_url: str


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SignInApi:
    """Обработчики /auth/*."""

    TAG: ClassVar[str] = "auth"

    def __init__(self, wiring: SignInWiring) -> None:
        self._wiring = wiring
        self._auth = wiring.auth
        self._cookie = SessionCookie(wiring.auth.cookie())

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            SignInUrl.PROVIDERS.value, self.providers, methods=["GET"], tags=[self.TAG]
        )
        router.add_api_route(
            SignInUrl.LOGIN.value,
            self.login,
            methods=["POST"],
            tags=[self.TAG],
            status_code=204,
        )
        router.add_api_route(
            SignInUrl.LOGOUT.value,
            self.logout,
            methods=["POST"],
            tags=[self.TAG],
            status_code=204,
        )
        router.add_api_route(
            SignInUrl.REFRESH.value,
            self.refresh,
            methods=["POST"],
            tags=[self.TAG],
            include_in_schema=False,
        )
        if not self._auth.providers().sso:
            return

        router.add_api_route(
            SignInUrl.SSO.value,
            self.sso,
            methods=["GET"],
            tags=[self.TAG],
            include_in_schema=False,
        )

    async def providers(self) -> SignInProviders:
        available = self._auth.providers()

        sso_url = ""
        if available.sso:
            sso_url = self._wiring.sso_url

        return SignInProviders(password=available.password, sso_url=sso_url)

    async def login(self, body: Credentials, request: Request) -> Response:
        self._own(request)
        session = await self._auth.by_password(body.username, body.password)

        response = Response(status_code=204)
        self._cookie.put(response, request.cookies, session.token)

        return response

    async def logout(self, request: Request) -> Response:
        self._own(request)
        response = Response(status_code=204)
        self._cookie.clear(response, request.cookies)

        return response

    @staticmethod
    def _own(request: Request) -> None:
        """Вход и выход меняют сессию: чужая форма без своей метки не пройдёт."""
        sso = SsoRequests.of(request)
        if sso.own_request:
            return

        msg = (
            f"{request.method} {request.url.path} from {sso.client}: "
            f"expected the own-request mark header, got none"
        )
        raise AuthorizationError(msg)

    def _next_of(self, raw: str | None) -> str:
        """Куда вернуть после входа: только внутрь страницы, иначе её начало."""
        page = self._wiring.page
        if raw is None:
            return page.home

        if raw == page.root or raw.startswith(f"{page.root}/"):
            return raw

        return page.home

    def _to_login(self, code: SsoErrorCode) -> RedirectResponse:
        return RedirectResponse(
            url=code.login_url(self._wiring.page.login), status_code=303
        )

    async def sso(self, request: Request, next: str | None = None) -> Response:  # noqa: A002
        """SPNEGO-вход: 401 Negotiate без токена, иначе строка users, cookie и 303."""
        try:
            outcome = await self._auth.by_spnego(SsoRequests.of(request))
        except AuthorizationError:
            return self._to_login(SsoErrorCode.DENIED)
        except (AuthenticationError, ExternalServiceError, InternalServiceError):
            return self._to_login(SsoErrorCode.FAILED)

        if isinstance(outcome, SsoChallenge):
            return SsoResponses.challenge(self._wiring.page.login)

        response = RedirectResponse(url=self._next_of(next), status_code=303)
        self._cookie.put(response, request.cookies, outcome.token)

        return response

    async def refresh(self, request: Request) -> Response:
        """Обновление живой сессии по её виду входа: 204 + новая cookie, 401 Negotiate
        (браузер повторит сам), 403 — сессию не продлить, страница уходит на вход.
        """
        token = self._cookie.token_of(request.cookies)
        outcome = await self._auth.refresh_session(SsoRequests.of(request), token)
        if isinstance(outcome, SsoRefused):
            return Response(status_code=403)

        if isinstance(outcome, SsoChallenge):
            return SsoResponses.silent_challenge()

        return self._issued(request, outcome)

    def _issued(self, request: Request, session: IssuedSession) -> Response:
        response = Response(status_code=204)
        self._cookie.put(response, request.cookies, session.token)

        return response
