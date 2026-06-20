import asyncio
from collections.abc import Awaitable, Callable

import chainlit as cl
from starlette.types import ASGIApp

from boba.chainlit2.chat.auth.ad import (
    ADDirectory,
    LDAPUnknownError,
    LDAPUserNotFoundErrorError,
)
from boba.chainlit2.chat.handler import chainlit_error_handler
from boba.chainlit2.errors import AuthenticationError, ExternalServiceError
from boba.chainlit2.infra.config import (
    LdapAuthConfig,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


class LdapAuth:
    """Логин/пароль с проверкой bind'ом в AD; роль — из групп AD (как kerberos)."""

    def __init__(self, c: LdapAuthConfig):
        self._provider = "ldap"
        self._c = c
        self._ad = ADDirectory

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        @chainlit_error_handler
        async def password_auth(username: str, password: str) -> cl.User | None:
            # личность подтверждаем bind'ом под пользователем, затем те же группы
            # (LDAP синхронный — в поток, чтобы не блокировать event loop)
            try:
                bind_dn = self._c.bind_dn_template.format(username=username)
                search_filter = self._c.user_filter.format(username=username)
                _user_dn, member_of = await asyncio.to_thread(
                    self._ad.fetch_userdn_and_member_of,
                    server=self._c.server,
                    bind_dn=bind_dn,
                    bind_password=password,
                    search_base=self._c.base_dn,
                    search_filter=search_filter,
                )

                roles = list(
                    await asyncio.to_thread(
                        self._ad.role_of,
                        group_dn_and_roles=self._c.group_role_map,
                        member_of=member_of,
                    )
                )

                return cl.User(
                    identifier=username,
                    metadata={
                        "role": roles,
                        "provider": self._provider,
                    },
                )
            except LDAPUserNotFoundErrorError as e:
                raise AuthenticationError("") from e
            except LDAPUnknownError as e:
                raise ExternalServiceError(
                    "ldap", "Couldn't perform ldap search"
                ) from e

        return password_auth
