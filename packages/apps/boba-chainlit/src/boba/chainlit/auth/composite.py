"""Вход по паролю в chainlit: сервис входа → cl.User для password-callback."""

from __future__ import annotations

import logging

import chainlit as cl
from boba.auth import AuthService
from boba.identity.errors import AuthenticationError
from chainlit.config import config as chainlit_config

__all__ = ["PasswordCallback"]

logger = logging.getLogger(__name__)


class PasswordCallback:
    """Ставит password_auth_callback chainlit поверх сервиса входа."""

    def __init__(self, auth: AuthService) -> None:
        self._auth = auth

    def install(self) -> None:
        # не cl.password_auth_callback — иначе chainlit глотает исключения
        chainlit_config.code.password_auth_callback = self.password_auth

    async def password_auth(self, username: str, password: str) -> cl.User | None:
        """None — chainlit отвечает своим 401; прочие ошибки входа идут наверх.

        Строку users и JWT chainlit заводит сам после колбэка, поэтому сервис
        входа зовётся без выпуска сессии.
        """
        try:
            signed = await self._auth.sign_in(username, password)
        except AuthenticationError as exc:
            # в лог идут логин и причина отказа, пароля здесь нет
            # nosemgrep: python-logger-credential-disclosure
            logger.info("password sign-in refused [user=%s]: %s", username, exc)
            return None

        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=signed.sign_in.render(),
        )
