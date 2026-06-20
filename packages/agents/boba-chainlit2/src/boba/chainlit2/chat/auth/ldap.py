import asyncio
from collections.abc import Awaitable, Callable

import chainlit as cl
from starlette.types import ASGIApp

from boba.chainlit2.chat.auth.ad import (
    ADDirectory,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
)
from boba.chainlit2.chat.handler import chainlit_error_handler
from boba.chainlit2.errors import (
    AuthenticationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.chainlit2.infra.config import (
    LdapAuthConfig,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


class LdapAuth:
    """
    Логин/пароль с проверкой bind'ом в AD
    роль забираем из групп AD
    """

    def __init__(self, c: LdapAuthConfig):
        self._provider = "ldap"
        self._c = c
        self._ad = ADDirectory

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        @chainlit_error_handler
        async def password_auth(username: str, password: str) -> cl.User | None:
            # личность подтверждаем bind'ом под пользователем
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
            except LDAPUserNotFoundError as e:
                raise AuthenticationError("User is not registered") from e
            except LDAPInvalidCredentialsError as e:
                raise AuthenticationError("Invalid username or password") from e
            except LDAPServerUnavailableError as e:
                raise ExternalServiceError(
                    "ldap", "LDAP is unavailable, please try again later"
                ) from e
            except LDAPError as e:
                # access denied / кривой конфиг / прочее — наша вина
                raise InternalServiceError(
                    internal_detail=f"ldap error: {e}", user_detail=None
                ) from e

        return password_auth
