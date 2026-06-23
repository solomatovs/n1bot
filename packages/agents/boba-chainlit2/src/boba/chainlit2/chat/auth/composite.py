from collections.abc import Awaitable, Callable

import chainlit as cl

from boba.chainlit2.chat.handler import chainlit_error_handler

UserCallback = Callable[..., Awaitable[cl.User | None]]


class PasswordAuthCallbackInstaller:
    def __init__(self):
        self._auth = []

    def fix_auth_setup(self, fix_auth) -> None:
        self._auth.append(fix_auth)

    def ldap_auth_setup(self, ldap_auth) -> None:
        self._auth.append(ldap_auth)

    def install_callback_if_any_exists(self) -> None:
        # устанавливаю callback только если есть авторизатор
        if self._auth:
            cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        @chainlit_error_handler
        async def password_auth(username: str, password: str) -> cl.User | None:
            for auth in self._auth:
                res = await auth.password_auth(username, password)
                if res is not None:
                    return res

            return None

        return password_auth
