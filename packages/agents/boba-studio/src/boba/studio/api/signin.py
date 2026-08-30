"""Вход через api: пароль и SPNEGO сервисом входа, JWT и cookie.

SSO: GET /auth/sso?next= — обмен на своём URL, после входа 303 на страницу;
POST /auth/sso/refresh — свежий билет для живой сессии.

Ошибки: свои не выпускает — ошибки сервиса входа (BaseError) переводит в HTTP
DomainErrorMiddleware; HTTPException 404 — SSO не настроен.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from fastapi import APIRouter, HTTPException, Request, Response
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
from boba.runtime.http import SsoRequests
from boba.studio.api.auth import SessionCookie
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
        if not self._auth.providers().sso:
            return

        router.add_api_route(
            SignInUrl.SSO.value,
            self.sso,
            methods=["GET"],
            tags=[self.TAG],
            include_in_schema=False,
        )
        router.add_api_route(
            SignInUrl.SSO_REFRESH.value,
            self.sso_refresh,
            methods=["GET", "POST"],
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
        session = await self._auth.by_password(body.username, body.password)

        response = Response(status_code=204)
        self._cookie.put(response, request.cookies, session.token)

        return response

    async def logout(self, request: Request) -> Response:
        response = Response(status_code=204)
        self._cookie.clear(response, request.cookies)

        return response

    def _require_sso(self) -> None:
        if not self._auth.providers().sso:
            raise HTTPException(status_code=404, detail="sso is not configured")

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
        self._require_sso()

        try:
            outcome = await self._auth.by_spnego(SsoRequests.of(request))
        except AuthorizationError:
            return self._to_login(SsoErrorCode.DENIED)
        except (AuthenticationError, ExternalServiceError, InternalServiceError):
            return self._to_login(SsoErrorCode.FAILED)

        if isinstance(outcome, SsoChallenge):
            return Response(
                content=SsoErrorCode.TICKET.challenge_page(self._wiring.page.login),
                status_code=401,
                headers=SsoChallenge.HEADERS,
                media_type="text/html",
            )

        response = RedirectResponse(url=self._next_of(next), status_code=303)
        self._cookie.put(response, request.cookies, outcome.token)

        return response

    async def sso_refresh(self, request: Request) -> Response:
        """Свежий билет для живой сессии: 204 + новая cookie, 401 Negotiate, 403."""
        self._require_sso()

        token = self._cookie.token_of(request.cookies)
        outcome = await self._auth.refresh(SsoRequests.of(request), token)
        if isinstance(outcome, SsoRefused):
            return Response(status_code=403)

        if isinstance(outcome, SsoChallenge):
            return Response(status_code=401, headers=SsoChallenge.HEADERS)

        return self._issued(request, outcome)

    def _issued(self, request: Request, session: IssuedSession) -> Response:
        response = Response(status_code=204)
        self._cookie.put(response, request.cookies, session.token)

        return response
