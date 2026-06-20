from collections.abc import Awaitable, Callable

import chainlit as cl
from starlette.types import ASGIApp

from boba.chainlit2.infra.config import (
    CredentialsAuthConfig,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


class CredentialsAuth:
    """Авторизация по статической таблице логин/пароль из конфига."""

    def __init__(self, c: CredentialsAuthConfig) -> None:
        self._users = dict(c.users)

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        users = self._users

        async def password_auth(username: str, password: str) -> cl.User | None:
            if users.get(username) == password:
                return cl.User(
                    identifier=username,
                    metadata={"role": "admin", "provider": "credentials"},
                )

            return None

        return password_auth
