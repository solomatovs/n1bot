from collections.abc import Awaitable, Callable

import chainlit as cl
from boba.chainlit.domain.errors import AuthenticationError
from chainlit.config import config as chainlit_config

UserCallback = Callable[..., Awaitable[cl.User | None]]


class PasswordAuthCallbackInstaller:
    def __init__(self):
        self._auth = []

    def local_auth_setup(self, local_auth) -> None:
        self._auth.append(local_auth)

    def ldap_auth_setup(self, ldap_auth) -> None:
        self._auth.append(ldap_auth)

    def install_callback_if_any_exists(self) -> None:
        if self._auth:
            # не cl.password_auth_callback — иначе chainlit глотает исключения
            chainlit_config.code.password_auth_callback = self._build_callback()

    def _build_callback(self) -> UserCallback:
        async def password_auth(username: str, password: str) -> cl.User | None:
            last_error: AuthenticationError | None = None

            for auth in self._auth:
                try:
                    res = await auth.password_auth(username, password)
                    if res is not None:
                        return res

                except AuthenticationError as e:
                    last_error = e

            if last_error:
                raise last_error

            return None

        return password_auth
