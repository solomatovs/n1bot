"""Вход через api: пароль и SPNEGO провайдерами services, строка users, JWT и cookie.

SSO: GET /auth/sso?next= — обмен на своём URL (общий SpnegoGate), после входа 303 на
страницу; POST /auth/sso/refresh — свежий билет для живой сессии.

Ошибки (HTTP):
401 — логин не зарегистрирован или пароль неверен.
403 — вход запрещён провайдером (исключение, нет ролей).
503 — каталог входа недоступен или вход по паролю не настроен.
500 — ошибка конфига каталога.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from boba.auth.sso import SpnegoGate
from boba.identity.api import UsersUpsert
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.signin import PasswordSignIn, SignedIn
from boba.identity.sso import SsoChallenge, SsoErrorCode, SsoRefused
from boba.identity.token import TokenIssuer
from boba.runtime.http import SsoRequests
from boba.studio.api.jwt_auth import JwtAuthenticator, SessionCookie
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
    """Что нужно входу: пароли (None — нет), SSO (None — нет kerberos), токены."""

    password: PasswordSignIn | None
    sso: SpnegoGate | None
    sso_url: str
    page: PageUrls
    issuer: TokenIssuer
    authenticator: JwtAuthenticator
    cookie: SessionCookie
    users: UsersUpsert


class SignInProviders(BaseModel):
    """Что доступно форме: пароль и/или адрес SSO chainlit (пусто — нет)."""

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
        if self._wiring.sso is None:
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
        sso_url = ""
        if self._wiring.sso is not None:
            sso_url = self._wiring.sso_url

        return SignInProviders(
            password=self._wiring.password is not None, sso_url=sso_url
        )

    async def login(self, body: Credentials, request: Request) -> Response:
        provider = self._wiring.password
        if provider is None:
            raise HTTPException(
                status_code=503, detail="password sign-in is not configured"
            )

        try:
            signed = await provider.sign_in(body.username, body.password)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExternalServiceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except InternalServiceError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if signed is None:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        await self._wiring.users.ensure_user(signed)
        token = self._wiring.issuer.issue(signed)

        response = Response(status_code=204)
        self._wiring.cookie.put(response, request.cookies, token)

        return response

    async def logout(self, request: Request) -> Response:
        response = Response(status_code=204)
        self._wiring.cookie.clear(response, request.cookies)

        return response

    def _gate(self) -> SpnegoGate:
        gate = self._wiring.sso
        if gate is None:
            raise HTTPException(status_code=404, detail="sso is not configured")

        return gate

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
        gate = self._gate()

        try:
            outcome = await gate.handshake(SsoRequests.of(request))
            if isinstance(outcome, SsoChallenge):
                gate.log_challenge(SsoRequests.of(request), outcome)
                return Response(
                    content=SsoErrorCode.TICKET.challenge_page(self._wiring.page.login),
                    status_code=401,
                    headers=SpnegoGate.NEGOTIATE,
                    media_type="text/html",
                )

            await self._wiring.users.ensure_user(outcome.signed)
        except AuthorizationError:
            return self._to_login(SsoErrorCode.DENIED)
        except (AuthenticationError, ExternalServiceError, InternalServiceError):
            return self._to_login(SsoErrorCode.FAILED)

        response = RedirectResponse(url=self._next_of(next), status_code=303)
        self._issue(request, response, outcome.signed)

        return response

    async def sso_refresh(self, request: Request) -> Response:
        """Свежий билет для живой сессии: 204 + новая cookie, 401 Negotiate, 403."""
        gate = self._gate()
        session = None
        if token := self._wiring.cookie.token_of(request.cookies):
            session = self._wiring.authenticator.ticket_of_token(token)

        outcome = await gate.refresh(SsoRequests.of(request), session)
        if isinstance(outcome, SsoRefused):
            gate.log_refusal(outcome)
            return Response(status_code=403)

        if isinstance(outcome, SsoChallenge):
            gate.log_challenge(SsoRequests.of(request), outcome)
            return Response(status_code=401, headers=SpnegoGate.NEGOTIATE)

        response = Response(status_code=204)
        self._issue(request, response, outcome.signed)

        return response

    def _issue(self, request: Request, response: Response, signed: SignedIn) -> None:
        token = self._wiring.issuer.issue(signed)
        self._wiring.cookie.put(response, request.cookies, token)
