"""Проверка токена сессии для HTTP-маршрутов AID."""

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer

from boba.auth import AuthService
from boba.identity.api import AuthenticatedUser
from boba.identity.errors import AuthenticationError
from boba.runtime import providers as runtime
from boba.runtime.di import Container
from boba.runtime.http import SessionCookie

_BEARER = HTTPBearer(auto_error=False)


async def authenticated_user(request: Request) -> AuthenticatedUser:
    """Проверяет cookie по подписанному токену без чтения БД."""
    container = Container.require_root("AID HTTP authentication")
    auth: AuthService = container.resolved(runtime.auth_service)
    cookie = SessionCookie(auth.cookie())
    token = cookie.token_of(request.cookies)

    if token is None:
        credentials = await _BEARER(request)
        if credentials is not None:
            token = credentials.credentials

    if token is None:
        raise HTTPException(status_code=401, detail="Sign-in required")

    try:
        return await auth.user_of_token(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Invalid session token") from exc
