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
        """None — chainlit отвечает своим 401; прочие ошибки входа идут наверх."""
        try:
            session = await self._auth.by_password(username, password)
        except AuthenticationError as exc:
            logger.info("password sign-in refused [user=%s]: %s", username, exc)
            return None

        return cl.User(
            identifier=session.signed.identifier,
            display_name=session.signed.display_name,
            metadata=dict(session.signed.metadata),
        )
