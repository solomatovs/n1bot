"""Ошибки kerberos-слоя.

Ошибки:
KeytabError — keytab недоступен, повреждён или не содержит принципала.
CredentialsExpiredError — тикет истёк и не продлевается.
DelegationNotPermittedError — делегирование запрещено политикой AD.
InvalidTokenError — клиент прислал непригодный SPNEGO-токен.
KerberosError — прочие сбои GSSAPI/krb5.
"""

from __future__ import annotations

from gssapi.exceptions import (
    ExpiredContextError,
    ExpiredCredentialsError,
    InvalidCredentialsError,
    MissingCredentialsError,
    UnauthorizedError,
)
from gssapi.raw.misc import GSSError

__all__ = [
    "CredentialsExpiredError",
    "DelegationNotPermittedError",
    "InvalidTokenError",
    "KerberosError",
    "KeytabError",
]


class KerberosError(Exception):
    """Базовая ошибка kerberos-слоя; наружу не выходит ничего сверх наследников."""

    @staticmethod
    def of_gss(exc: GSSError, context: str) -> KerberosError:
        """Классифицирует GSSError в ошибку слоя."""
        if isinstance(exc, (ExpiredCredentialsError, ExpiredContextError)):
            return CredentialsExpiredError(f"{context}: {exc}")

        if isinstance(exc, UnauthorizedError):
            return DelegationNotPermittedError(f"{context}: {exc}")

        if isinstance(exc, (MissingCredentialsError, InvalidCredentialsError)):
            return KeytabError(f"{context}: {exc}")

        return KerberosError(
            f"{context}: gss maj={exc.maj_code} min={exc.min_code}: {exc}"
        )


class KeytabError(KerberosError):
    """keytab недоступен, повреждён или не содержит нужного принципала."""


class CredentialsExpiredError(KerberosError):
    """Тикет истёк; требуется повторное получение из keytab или повторный логин."""


class DelegationNotPermittedError(KerberosError):
    """Делегирование запрещено политикой AD (msDS-AllowedToDelegateTo)."""


class InvalidTokenError(KerberosError):
    """Клиент прислал битый, просроченный или неполный SPNEGO-токен."""
