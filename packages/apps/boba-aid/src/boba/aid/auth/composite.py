"""Парольный вход и проверка сессии Chainlit через общий AuthService.

Отказ входа уходит браузеру кодом ошибки (detail ответа /login): страница
входа и уведомление фронта показывают по нему строку переводов chainlit
auth.login.errors.<код>, в журнал идёт причина целиком.

Ошибки:
HTTPException — 401 неверные credentials или отсутствующий пользователь,
    403 вход запрещён, 503 каталог или БД недоступны, 500 внутренний сбой.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated

import chainlit as cl
import chainlit.auth as chainlit_auth
from chainlit.config import config as chainlit_config
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, OAuth2PasswordRequestForm

from boba.aid.auth.chainlit_user import ChainlitPrincipal
from boba.aid.auth.refresh import SessionRefresh
from boba.aid.infra.session import ChainlitSessions
from boba.auth import AuthService, IssuedSession
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.session import UserMetadataField
from boba.runtime.http import SessionCookie

__all__ = ["PasswordCallback", "SessionCallback", "SignInCode"]

logger = logging.getLogger(__name__)


class SignInCode(StrEnum):
    """Коды отказа входа по паролю — ключи auth.login.errors переводов chainlit."""

    CREDENTIALS = "credentialssignin"
    """Неверный логин или пароль; тот же код chainlit даёт сам."""

    DENIED = "access_denied"
    UNAVAILABLE = "service_unavailable"
    INTERNAL = "internal_error"

    def refusal(self) -> HTTPException:
        codes = {
            SignInCode.CREDENTIALS: status.HTTP_401_UNAUTHORIZED,
            SignInCode.DENIED: status.HTTP_403_FORBIDDEN,
            SignInCode.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
            SignInCode.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
        }
        return HTTPException(status_code=codes[self], detail=self.value)


class SessionCallback:
    """Адаптер AuthService для проверки JWT в HTTP и сокетах Chainlit.

    Устанавливается ChainlitAuthInstaller. Проверка токена локальная; UUID,
    роли и identity берутся из подписанных claims, без чтения слоя данных.
    """

    def __init__(self, auth: AuthService, sessions: ChainlitSessions) -> None:
        self._auth = auth
        self._sessions = sessions
        self._bearer = HTTPBearer(auto_error=False)
        self._cookie = SessionCookie(auth.cookie())

    def install(self, app: FastAPI) -> None:
        chainlit_auth.authenticate_user = self.authenticate

        SessionRefresh.prepend_route(app, "/auth/jwt", self.submit, methods=["POST"])

    async def submit(self, request: Request) -> JSONResponse:
        credentials = await self._bearer(request)
        if credentials is None:
            raise SignInCode.CREDENTIALS.refusal()

        await self.authenticate(credentials.credentials)
        response = JSONResponse({"success": True})
        self._cookie.put(response, request.cookies, credentials.credentials)

        return response

    async def authenticate(self, token: str) -> cl.PersistedUser:
        try:
            session = await self._auth.session_of_token(token)
        except AuthenticationError as exc:
            raise SignInCode.CREDENTIALS.refusal() from exc

        persisted = ChainlitPrincipal.project(
            session.user, display_name=session.signed.display_name
        )
        self._sessions.refresh_user(token, persisted)

        return persisted


class PasswordCallback:
    """Ставит password_auth_callback chainlit поверх сервиса входа."""

    def __init__(self, auth: AuthService) -> None:
        self._auth = auth

    def install(self, app: FastAPI) -> None:
        # не cl.password_auth_callback — иначе chainlit глотает исключения
        chainlit_config.code.password_auth_callback = self.password_auth
        SessionRefresh.prepend_route(app, "/login", self.submit, methods=["POST"])

    async def submit(
        self,
        request: Request,
        form: Annotated[OAuth2PasswordRequestForm, Depends()],
        realm: Annotated[str, Form()] = "",
    ) -> JSONResponse:
        session = await self._session(form.username, form.password, realm)
        response = JSONResponse({"success": True})
        SessionCookie(self._auth.cookie()).put(response, request.cookies, session.token)

        return response

    async def password_auth(self, username: str, password: str) -> cl.User:
        session = await self._session(username, password)
        signed = session.signed
        metadata = signed.sign_in.render()
        metadata[UserMetadataField.USER_ID] = str(session.user.id)
        metadata[UserMetadataField.CREATED_AT] = session.user.created_at.isoformat()

        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=metadata,
        )

    async def _session(
        self, username: str, password: str, realm: str = ""
    ) -> IssuedSession:
        """Сессия общего сервиса входа; отказ — HTTPException с кодом отказа."""
        try:
            return await self._auth.by_password(username, password, realm)
        except AuthenticationError as exc:
            logger.info("password sign-in refused")
            raise SignInCode.CREDENTIALS.refusal() from exc
        except AuthorizationError as exc:
            logger.info("password sign-in denied")
            raise SignInCode.DENIED.refusal() from exc
        except ExternalServiceError as exc:
            logger.error("password sign-in unavailable")
            raise SignInCode.UNAVAILABLE.refusal() from exc
        except InternalServiceError as exc:
            logger.error("password sign-in failed")
            raise SignInCode.INTERNAL.refusal() from exc
