"""Вход по паролю в chainlit: сервис входа → cl.User для password-callback.

Отказ входа уходит браузеру кодом ошибки (detail ответа /login): страница
входа и уведомление фронта показывают по нему строку переводов chainlit
auth.login.errors.<код>, в журнал идёт причина целиком.

Ошибки:
HTTPException — вход отклонён: 401 неверный логин или пароль, 403 нет ролей,
    503 каталог входа недоступен, 500 внутренний сбой входа.
"""

from __future__ import annotations

import logging
from enum import StrEnum

import chainlit as cl
from chainlit.config import config as chainlit_config
from fastapi import HTTPException, status

from boba.auth import AuthService
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)

__all__ = ["PasswordCallback", "SignInCode"]

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


class PasswordCallback:
    """Ставит password_auth_callback chainlit поверх сервиса входа."""

    def __init__(self, auth: AuthService) -> None:
        self._auth = auth

    def install(self) -> None:
        # не cl.password_auth_callback — иначе chainlit глотает исключения
        chainlit_config.code.password_auth_callback = self.password_auth

    async def password_auth(self, username: str, password: str) -> cl.User:
        """Пользователь входа; отказ — HTTPException с кодом отказа.

        Строку users и JWT chainlit заводит сам после колбэка, поэтому сервис
        входа зовётся без выпуска сессии. В лог идут логин и причина отказа,
        пароля здесь нет.
        """
        try:
            signed = await self._auth.sign_in(username, password)
        except AuthenticationError as exc:
            # nosemgrep: python-logger-credential-disclosure
            logger.info("password sign-in refused [user=%s]: %s", username, exc)
            raise SignInCode.CREDENTIALS.refusal() from exc
        except AuthorizationError as exc:
            # nosemgrep: python-logger-credential-disclosure
            logger.info("password sign-in denied [user=%s]: %s", username, exc)
            raise SignInCode.DENIED.refusal() from exc
        except ExternalServiceError as exc:
            # nosemgrep: python-logger-credential-disclosure
            logger.error("password sign-in unavailable [user=%s]: %s", username, exc)
            raise SignInCode.UNAVAILABLE.refusal() from exc
        except InternalServiceError as exc:
            # nosemgrep: python-logger-credential-disclosure
            logger.error("password sign-in failed [user=%s]: %s", username, exc)
            raise SignInCode.INTERNAL.refusal() from exc

        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=signed.sign_in.render(),
        )
