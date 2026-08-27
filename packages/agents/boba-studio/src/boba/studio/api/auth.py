"""Вход запроса API: токен из cookie либо Authorization, пользователь — порт входа.

Ошибки (HTTP):
401 — токен негоден, вход не сохранён или id пользователя не число.
403 — профиль недоступен ролям пользователя.
RuntimeError — ApiAuth не установлен в приложение (ошибка сборки).
"""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Annotated, Any, ClassVar

from fastapi import Depends, FastAPI, HTTPException, Request

from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject, AuthenticatedUser, Authenticator
from boba.identity.errors import AuthenticationError, RefusalError

__all__ = ["ApiAuth", "ApiIdentity", "CurrentUser", "TokenReader"]


class TokenReader:
    """Токен входа: cookie целиком либо чанками name_0..name_n, иначе Bearer."""

    BEARER: ClassVar[str] = "Bearer "
    COOKIE_HEADER: ClassVar[str] = "HTTP_COOKIE"
    AUTHORIZATION: ClassVar[str] = "Authorization"

    def __init__(self, cookie: str) -> None:
        self._cookie = cookie

    def of_cookies(self, cookies: Mapping[str, str]) -> str | None:
        whole = cookies.get(self._cookie)
        if whole:
            return whole

        parts: list[str] = []
        index = 0
        while True:
            chunk = cookies.get(f"{self._cookie}_{index}")
            if chunk is None:
                break

            parts.append(chunk)
            index += 1

        joined = "".join(parts)
        if not joined:
            return None

        return joined

    def of_request(self, request: Request) -> str | None:
        token = self.of_cookies(request.cookies)
        if token is not None:
            return token

        header = request.headers.get(self.AUTHORIZATION)
        if not header:
            return None

        if not header.startswith(self.BEARER):
            return None

        return header[len(self.BEARER) :]

    def of_environ(self, environ: Mapping[str, Any]) -> str | None:
        """Cookie из WSGI/ASGI environ подключения socket.io."""
        raw = environ.get(self.COOKIE_HEADER)
        if not isinstance(raw, str):
            return None

        if not raw:
            return None

        parsed = SimpleCookie()
        parsed.load(raw)

        cookies: dict[str, str] = {}
        for name, morsel in parsed.items():
            cookies[name] = morsel.value

        return self.of_cookies(cookies)


class ApiAuth:
    """Пользователь входа по запросу или environ сокета; живёт в state приложения."""

    STATE_KEY: ClassVar[str] = "api_auth"

    def __init__(self, authenticator: Authenticator, tokens: TokenReader) -> None:
        self._authenticator = authenticator
        self._tokens = tokens

    async def user_of_request(self, request: Request) -> AuthenticatedUser | None:
        token = self._tokens.of_request(request)
        if token is None:
            return None

        return await self._authenticator.user_of_token(token)

    async def user_of_environ(
        self, environ: Mapping[str, Any]
    ) -> AuthenticatedUser | None:
        token = self._tokens.of_environ(environ)
        if token is None:
            return None

        return await self._authenticator.user_of_token(token)

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
    async def current(request: Request) -> AuthenticatedUser | None:
        """Зависимость FastAPI: пользователь входа текущего запроса."""
        return await ApiAuth.of_app(request.app).user_of_request(request)


CurrentUser = Annotated[AuthenticatedUser | None, Depends(ApiAuth.current)]


class ApiIdentity:
    """Субъект вызова API: 401 без сохранённого входа, 403 если профиль недоступен."""

    @staticmethod
    def resolve(
        user: AuthenticatedUser | None, profile: str | None, profiles: ChatProfiles
    ) -> ApiSubject:
        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            selected = profiles.resolve_or_default(profile, user.roles).name
        except RefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        try:
            return ApiSubject.of(user, selected)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
