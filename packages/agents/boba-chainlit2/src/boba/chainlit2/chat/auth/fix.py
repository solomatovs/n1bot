from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal

import chainlit as cl
from pydantic import BaseModel, ConfigDict, Field, RootModel
from starlette.types import ASGIApp

from boba.chainlit2.chat.handler import chainlit_error_handler

UserCallback = Callable[..., Awaitable[cl.User | None]]


class RolesMappingConfig(RootModel[dict[str, list[str]]]):
    """Фиксированный мапер пользователь - список ролей"""

    def roles_of(self, key: str) -> list[str]:
        return self.root.get(key, [])


class FixUserRolesProvider:
    """Фиксированный провайдер пользователь - список ролей"""

    def __init__(self, mapping: RolesMappingConfig):
        self._mapping = mapping

    def roles_of(self, username: str) -> Iterable[str]:
        yield from self._mapping.roles_of(username)


class FixAuthConfig(BaseModel):
    """Авторизация по статической таблице логин/пароль из конфига."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["fix"] = "fix"

    users: dict[str, str] = Field(
        default_factory=dict,
        description="Таблица логин→пароль; совпадение выдаёт роль admin.",
    )

    roles: RolesMappingConfig | None = Field(
        default=None,
        description="Источник ролей для пользователей",
    )


class FixAuth:
    """Авторизация по статической таблице логин/пароль из конфига."""

    def __init__(self, config: FixAuthConfig) -> None:
        self._config = config
        self._init_mapping()

    def _init_mapping(self):
        self._fixed_roles: FixUserRolesProvider | None = None
        if roles := self._config.roles:
            self._fixed_roles = FixUserRolesProvider(roles)

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        users = self._config.users

        @chainlit_error_handler
        async def password_auth(username: str, password: str) -> cl.User | None:
            if users.get(username) == password:
                metadata: dict[str, Any] = {"provider": FixAuth.__name__}

                roles: list[str] = []
                if self._fixed_roles:
                    roles.extend(self._fixed_roles.roles_of(username))

                roles = list(set(roles))

                if roles:
                    metadata.update(roles=roles)

                return cl.User(
                    identifier=username,
                    display_name=username,
                    metadata=metadata,
                )

            return None

        return password_auth
