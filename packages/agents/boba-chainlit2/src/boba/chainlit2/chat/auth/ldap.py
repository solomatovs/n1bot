import asyncio
import logging
from collections.abc import Awaitable, Callable

import chainlit as cl
from starlette.types import ASGIApp

from boba.chainlit2.infra.config import (
    LdapAuthConfig,
)

from .ad import ADDirectory, LDAPUserNotFoundErrorError

UserCallback = Callable[..., Awaitable[cl.User | None]]


class LdapAuth:
    """Логин/пароль с проверкой bind'ом в AD; роль — из групп AD (как kerberos)."""

    def __init__(self, c: LdapAuthConfig):
        self._provider = "ldap"
        self._c = c
        self._activedirectory = ADDirectory
        self._logger = logging.getLogger(LdapAuth.__name__)

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        async def password_auth(username: str, password: str) -> cl.User | None:
            # личность подтверждаем bind'ом под пользователем, затем те же группы
            # (LDAP синхронный — в поток, чтобы не блокировать event loop)
            try:
                bind_dn = self._c.bind_dn_template.format(username=username)
                search_filter = self._c.user_filter.format(username=username)
                _user_dn, member_of = await asyncio.to_thread(
                    self._activedirectory.fetch_userdn_and_member_of,
                    server=self._c.server,
                    bind_dn=bind_dn,
                    bind_password=password,
                    search_base=self._c.base_dn,
                    search_filter=search_filter,
                )

                roles = list(
                    await asyncio.to_thread(
                        self._activedirectory.role_of,
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
            except LDAPUserNotFoundErrorError as _e:
                return None
            except Exception:
                self._logger.exception("Couldn't perform ldap search")
                return None

        return password_auth
