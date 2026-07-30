from collections.abc import Iterable
from typing import Any, Literal

import chainlit as cl
from pydantic import BaseModel, ConfigDict, Field, RootModel

from boba.auth.errors import AuthorizationError


class RoleMappingConfig(RootModel[dict[str, list[str]]]):
    """Фиксированный мапер пользователь - список ролей"""

    def roles_of(self, key: str) -> list[str]:
        return self.root.get(key, [])


class RoleExcludeConfig(RootModel[list[str]]):
    """Фиксированный список исключённых пользователей/ролей"""

    def exclude_of(self, key: str) -> Iterable[bool]:
        for x in self.root:
            yield x == key


class LocalUserRolesProvider:
    """Локальный провайдер пользователь - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, username: str) -> Iterable[str]:
        yield from self._mapping.roles_of(username)


class LocalExcludeUserProvider:
    """Локальный список пользователей, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, username: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(username)


class LocalAuthConfig(BaseModel):
    """Авторизация по статической таблице логин/пароль из конфига."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["local"] = "local"

    users: dict[str, str] = Field(
        default_factory=dict,
        description="Таблица логин→пароль; совпадение выдаёт роль admin.",
    )

    roles: RoleMappingConfig | None = Field(
        default=None,
        description="Источник ролей для пользователей",
    )

    roles_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Список логинов, которым запрещён вход (403).",
    )

    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )


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
        if self._config.users.get(username) == password:
            if self._local_roles_ex and any(self._local_roles_ex.exclude_of(username)):
                raise AuthorizationError("Access denied")

            metadata: dict[str, Any] = {"provider": LocalAuth.__name__}

            roles: list[str] = []
            if self._local_roles:
                roles.extend(self._local_roles.roles_of(username))

            roles = list(set(roles))

            if self._config.require_roles and not roles:
                raise AuthorizationError("Access denied")

            if roles:
                metadata.update(roles=roles)

            return cl.User(
                identifier=username,
                display_name=username,
                metadata=metadata,
            )

        return None
