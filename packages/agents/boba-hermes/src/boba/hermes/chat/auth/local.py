from typing import Any, Literal

import chainlit as cl
from pydantic import BaseModel, ConfigDict, Field

from boba.hermes.chat.auth.roles import (
    RoleExcludeConfig,
    RoleMappingConfig,
    StaticExcludeProvider,
    StaticRolesProvider,
)
from boba.hermes.errors import AuthorizationError


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
        self._static_roles: StaticRolesProvider | None = None
        self._static_roles_ex: StaticExcludeProvider | None = None

        if roles := self._config.roles:
            self._static_roles = StaticRolesProvider(roles)

        if roles_ex := self._config.roles_ex:
            self._static_roles_ex = StaticExcludeProvider(roles_ex)

    async def password_auth(self, username: str, password: str) -> cl.User | None:
        if self._config.users.get(username) == password:
            excluded = self._static_roles_ex
            if excluded and any(excluded.exclude_of(username)):
                raise AuthorizationError("Access denied")

            metadata: dict[str, Any] = {"provider": LocalAuth.__name__}

            roles: list[str] = []
            if self._static_roles:
                roles.extend(self._static_roles.roles_of(username))

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
