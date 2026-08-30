"""Каталог пользователей: запись пользователя, параметры обращения, порт поиска и
ошибки каталога — без клиента.

Ошибки (выпускают реализации UserDirectory):
LDAPServerUnavailableError — каталог недоступен: сеть, TLS, таймаут.
LDAPInvalidCredentialsError — bind отклонён: неверные креды пользователя или
    сервисной учётки.
LDAPAccessDeniedError — недостаточно прав на операцию.
LDAPConfigError — несуществующий base DN, неверный DN, фильтр, сервер или TLS.
LDAPUserNotFoundError — поиск выполнен, записи пользователя нет.
LDAPError — прочие сбои каталога.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

__all__ = [
    "ADUserEntry",
    "DirectoryBinding",
    "DirectorySearch",
    "LDAPAccessDeniedError",
    "LDAPConfigError",
    "LDAPError",
    "LDAPInvalidCredentialsError",
    "LDAPServerUnavailableError",
    "LDAPUserNotFoundError",
    "UserDirectory",
]


class LDAPError(Exception):
    "База ошибок каталога; транспортно-нейтральна, домен мапят вызывающие."


class LDAPServerUnavailableError(LDAPError):
    "Каталог недоступен (сокет/сеть/TLS/таймаут) — не наша вина."


class LDAPInvalidCredentialsError(LDAPError):
    "bind отклонён: неверные креды (юзер или сервис-аккаунт — решает вызывающий)."


class LDAPAccessDeniedError(LDAPError):
    "Недостаточно прав на операцию (insufficient access)."


class LDAPConfigError(LDAPError):
    "Кривой конфиг: несуществующий base DN, неверный DN/фильтр/сервер/TLS-политика."


class LDAPUserNotFoundError(LDAPError):
    "Поиск выполнен, но запись пользователя не найдена."


@dataclass(frozen=True)
class ADUserEntry:
    """Атрибуты пользователя из AD для маппинга ролей/исключений."""

    dn: str
    samaccountname: str
    member_of: list[str]


class DirectoryBinding(BaseModel):
    """Кем и куда подключаться: сервер и bind-учётка (пользователь либо сервисная)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str = Field(min_length=1)
    bind_dn: str = Field(min_length=1)
    bind_password: SecretStr


class DirectorySearch(BaseModel):
    """Где и кого искать: база поиска и LDAP-фильтр одного пользователя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_dn: str = Field(min_length=1)
    filter: str = Field(min_length=1)


class UserDirectory(Protocol):
    """Поиск одного пользователя в каталоге под указанной bind-учёткой."""

    @abstractmethod
    async def find(
        self, binding: DirectoryBinding, search: DirectorySearch
    ) -> ADUserEntry:
        """Запись пользователя; ошибки — семейство LDAPError."""
