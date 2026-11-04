"""Настройки способов входа с реестром, который передаёт приложение.

Ошибки:
ValueError — настройки входа не соответствуют подключённым бэкендам.
"""

from typing import ClassVar, TypeVar

from pydantic import SerializeAsAny, field_validator

from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.config import ConfigModel

AuthConfigT = TypeVar("AuthConfigT", bound=AuthConfig)


class SignInSettings(ConfigModel):
    """Поле auth и его проверка; приложения объявляют собственные остальные секции."""

    AUTH_BACKENDS: ClassVar[AuthBackends] = AuthBackends(())
    auth: list[SerializeAsAny[AuthConfig]]

    @field_validator("auth", mode="before")
    @classmethod
    def _auth_backends(cls, raw: object) -> list[AuthConfig]:
        if not isinstance(raw, (list, tuple)):
            raise ValueError("[auth] expects a sequence of backend configurations")

        configs = [cls.AUTH_BACKENDS.parse(value) for value in raw]
        cls.AUTH_BACKENDS.validate(configs)

        return configs

    def auth_entries(self, model: type[AuthConfigT]) -> tuple[AuthConfigT, ...]:
        entries = (entry for entry in self.auth if isinstance(entry, model))

        return tuple(entries)
