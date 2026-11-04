"""Проверка токена сессии для HTTP-маршрутов AID."""

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer

from boba.aid.auth.http import SessionCookie
from boba.aid.auth.session import BrowserSessions
from boba.aid.infra import services
from boba.aid.infra.di import Container
from boba.identity.authentication import AuthenticatedPrincipal
from boba.identity.errors import AuthenticationError

_BEARER = HTTPBearer(auto_error=False)


async def authenticated_user(request: Request) -> AuthenticatedPrincipal:
    """Проверяет cookie по подписанному токену без чтения БД."""
    container = Container.require_root("AID HTTP authentication")
    auth: BrowserSessions = container.resolved(services.auth_service)
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
