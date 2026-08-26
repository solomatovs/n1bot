"""Каталог пользователей: запись пользователя и ошибки каталога, без клиента.

Ошибки:
LDAPError и наследники — база для клиентов каталога; домен мапят вызывающие.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ADUserEntry",
    "LDAPAccessDeniedError",
    "LDAPConfigError",
    "LDAPError",
    "LDAPInvalidCredentialsError",
    "LDAPServerUnavailableError",
    "LDAPUserNotFoundError",
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
