"""Вход запроса API: токен из cookie либо Authorization, пользователь — порт входа;
cookie входа в ответе.

Ошибки:
AuthenticationError — токена нет, он негоден или вход не сохранён.
HTTPException 403 — профиль недоступен ролям пользователя.
RuntimeError — ApiAuth не установлен в приложение (ошибка сборки).
"""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Annotated, Any, ClassVar

from fastapi import Depends, FastAPI, Request, Response

from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject, AuthenticatedUser, Authenticator
from boba.identity.errors import AuthenticationError, AuthorizationError, RefusalError
from boba.identity.token import CookieJar, CookieSpec

__all__ = [
    "ApiAuth",
    "CurrentSubject",
    "CurrentUser",
    "RequestTokens",
    "SessionCookie",
]


class RequestTokens:
    """Токен входа запроса: cookie (целиком либо чанками), иначе Bearer."""

    BEARER: ClassVar[str] = "Bearer "
    COOKIE_HEADER: ClassVar[str] = "HTTP_COOKIE"
    AUTHORIZATION: ClassVar[str] = "Authorization"

    def __init__(self, cookie: str) -> None:
        self._jar = CookieJar(cookie)

    def of_cookies(self, cookies: Mapping[str, str]) -> str | None:
        return self._jar.token_of(cookies)

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

    async def user_of_environ(
        self, environ: Mapping[str, Any]
    ) -> AuthenticatedUser | None:
        """Пользователь подключения сокета; None — подключение без входа."""
        token = self._tokens.of_environ(environ)
        if token is None:
            return None

        try:
            return await self._authenticator.user_of_token(token)
        except AuthenticationError:
            return None

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


class SessionCookie:
    """Cookie входа в ответе HTTP: атрибуты из CookieSpec, чанки — CookieJar."""

    def __init__(self, spec: CookieSpec) -> None:
        self._spec = spec
        self._jar = CookieJar(spec.name)

    @property
    def jar(self) -> CookieJar:
        return self._jar

    def put(self, response: Response, present: Mapping[str, str], token: str) -> None:
        """Ставит токен и снимает чанки прежнего, более длинного токена."""
        pieces = self._jar.pieces(token)
        for key, value in pieces:
            self._set(response, key, value)

        for key in self._jar.stale(present, pieces):
            self._delete(response, key)

    def token_of(self, present: Mapping[str, str]) -> str | None:
        """Токен из cookie запроса: целиком либо из чанков."""
        return self._jar.token_of(present)

    def clear(self, response: Response, present: Mapping[str, str]) -> None:
        for key in self._jar.ours(present):
            self._delete(response, key)

    def _set(self, response: Response, key: str, value: str) -> None:
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
            max_age=self._spec.ttl_sec,
            path=self._spec.path,
        )

    def _delete(self, response: Response, key: str) -> None:
        response.delete_cookie(
            key=key,
            path=self._spec.path,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
        )
