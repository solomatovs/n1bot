"""Ошибки kerberos: одна база и её виды, которые поднимают infra и сервисы.

Ошибки:
KerberosError — сбой GSSAPI/krb5, принципал неизвестен до вызова, билет без SPN.
TicketSealError — запечатанный билет входа не читается: чужой ключ, порча
    или не тот формат.
KeytabError — keytab недоступен, повреждён или не содержит принципала.
CredentialsExpiredError — тикет истёк и не продлевается.
DelegationNotPermittedError — делегирование запрещено политикой AD.
InvalidTokenError — клиент прислал непригодный SPNEGO-токен.
"""

from __future__ import annotations

__all__ = [
    "CredentialsExpiredError",
    "DelegationNotPermittedError",
    "InvalidTokenError",
    "KerberosError",
    "KeytabError",
    "TicketSealError",
]


class KerberosError(Exception):
    """База ошибок kerberos: инфраструктурные варианты наследуют её."""


class TicketSealError(KerberosError):
    """Запечатанный билет входа не открывается: чужой ключ, порча или не тот формат."""


class KeytabError(KerberosError):
    """keytab недоступен, повреждён или не содержит нужного принципала."""


class CredentialsExpiredError(KerberosError):
    """Тикет истёк; требуется повторное получение из keytab или повторный логин."""


class DelegationNotPermittedError(KerberosError):
    """Делегирование запрещено политикой AD (msDS-AllowedToDelegateTo)."""


class InvalidTokenError(KerberosError):
    """Клиент прислал битый, просроченный или неполный SPNEGO-токен."""
