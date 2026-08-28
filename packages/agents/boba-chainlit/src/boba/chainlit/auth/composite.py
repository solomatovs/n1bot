"""Вход по паролю в chainlit: провайдер services → cl.User для password-callback."""

from __future__ import annotations

import chainlit as cl
from boba.identity.signin import PasswordSignIn
from chainlit.config import config as chainlit_config

__all__ = ["PasswordCallback"]


class PasswordCallback:
    """Ставит password_auth_callback chainlit поверх провайдера паролей."""

    def __init__(self, signin: PasswordSignIn) -> None:
        self._signin = signin

    def install(self) -> None:
        # не cl.password_auth_callback — иначе chainlit глотает исключения
        chainlit_config.code.password_auth_callback = self.password_auth

    async def password_auth(self, username: str, password: str) -> cl.User | None:
        signed = await self._signin.sign_in(username, password)
        if signed is None:
            return None

        return cl.User(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=dict(signed.metadata),
        )
