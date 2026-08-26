from typing import Any

import chainlit as cl
from boba.chainlit.auth.config import LocalAuthConfig
from boba.identity.errors import AuthorizationError
from boba.identity.roles import (
    LocalExcludeUserProvider,
    LocalUserRolesProvider,
)
from boba.identity.session import SignInProvider, UserLogin, UserMetadataField


class LocalAuth:
    """Авторизация по статической таблице логин/пароль из конфига."""

    def __init__(self, config: LocalAuthConfig) -> None:
        self._config = config
        self._init_mapping()

    def _init_mapping(self):
        self._local_roles: LocalUserRolesProvider | None = None
        self._local_roles_ex: LocalExcludeUserProvider | None = None

        if roles := self._config.roles:
            self._local_roles = LocalUserRolesProvider(roles)

        if roles_ex := self._config.roles_ex:
            self._local_roles_ex = LocalExcludeUserProvider(roles_ex)

    async def password_auth(self, username: str, password: str) -> cl.User | None:
        if self._config.users.get(username) != password:
            return None

        excluded = False
        if self._local_roles_ex:
            excluded = any(self._local_roles_ex.exclude_of(username))

        if excluded:
            raise AuthorizationError("Access denied")

        metadata: dict[str, Any] = {
            UserMetadataField.PROVIDER: SignInProvider.LOCAL.value,
        }

        roles: list[str] = []
        if self._local_roles:
            roles.extend(self._local_roles.roles_of(username))

        roles = sorted(set(roles))

        requires_roles = self._config.require_roles
        if requires_roles and not roles:
            raise AuthorizationError("Access denied")

        if roles:
            metadata[UserMetadataField.ROLES] = roles

        login = UserLogin.of(username)

        return cl.User(
            identifier=login.key,
            display_name=login.display,
            metadata=metadata,
        )
