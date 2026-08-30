"""Вход запроса API: токен из cookie либо Authorization, пользователь — порт входа;
cookie входа в ответе.

Ошибки:
AuthenticationError — токена нет, он негоден или вход не сохранён.
HTTPException 403 — профиль недоступен ролям пользователя.
RuntimeError — ApiAuth не установлен в приложение (ошибка сборки).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject, AuthenticatedUser, Authenticator
from boba.identity.errors import AuthenticationError, AuthorizationError, RefusalError
from boba.runtime.http import RequestTokens

__all__ = [
    "ApiAuth",
    "CurrentSubject",
    "CurrentUser",
    "SocketSignIn",
]


class SocketSignIn(BaseModel):
    """Вход подключившегося сокета: пользователь и токен, которым он вошёл."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: AuthenticatedUser
    token: str = Field(min_length=1)


class ApiAuth:
    """Пользователь входа по запросу или environ сокета; живёт в state приложения."""

    STATE_KEY: ClassVar[str] = "api_auth"

    def __init__(
        self,
        authenticator: Authenticator,
        tokens: RequestTokens,
        profiles: ChatProfiles,
    ) -> None:
        self._authenticator = authenticator
        self._tokens = tokens
        self._profiles = profiles

    @staticmethod
    def resolve(
        user: AuthenticatedUser, profile: str | None, profiles: ChatProfiles
    ) -> ApiSubject:
        """Субъект под профилем: AuthorizationError — профиль недоступен ролям."""
        try:
            selected = profiles.resolve_or_default(profile, user.roles).name
        except RefusalError as exc:
            raise AuthorizationError(str(exc)) from exc

        return ApiSubject.of(user, selected)

    async def subject_of_request(
        self, request: Request, profile: str | None
    ) -> ApiSubject:
        user = await self.user_of_request(request)

        return self.resolve(user, profile, self._profiles)

    async def user_of_request(self, request: Request) -> AuthenticatedUser:
        token = self._tokens.of_request(request)
        if token is None:
            raise AuthenticationError("request carries no sign-in token")

        return await self._authenticator.user_of_token(token)

    async def socket_sign_in(self, environ: Mapping[str, Any]) -> SocketSignIn | None:
        """Вход подключения сокета; None — без входа или с негодным токеном."""
        token = self._tokens.of_environ(environ)
        if token is None:
            return None

        try:
            user = await self._authenticator.user_of_token(token)
        except AuthenticationError:
            return None

        return SocketSignIn(user=user, token=token)

    def install(self, app: FastAPI) -> None:
        setattr(app.state, self.STATE_KEY, self)

    @classmethod
    def of_app(cls, app: Any) -> ApiAuth:
        auth = getattr(app.state, cls.STATE_KEY, None)
        if not isinstance(auth, ApiAuth):
            msg = "api auth is not installed on the application"
            raise RuntimeError(msg)

        return auth

    @staticmethod
    async def current(request: Request) -> AuthenticatedUser:
        """Зависимость FastAPI: пользователь входа текущего запроса."""
        return await ApiAuth.of_app(request.app).user_of_request(request)

    @staticmethod
    async def subject(request: Request, profile: str | None = None) -> ApiSubject:
        """Зависимость FastAPI: субъект текущего запроса под профилем из ?profile=."""
        return await ApiAuth.of_app(request.app).subject_of_request(request, profile)


CurrentUser = Annotated[AuthenticatedUser, Depends(ApiAuth.current)]
CurrentSubject = Annotated[ApiSubject, Depends(ApiAuth.subject)]
