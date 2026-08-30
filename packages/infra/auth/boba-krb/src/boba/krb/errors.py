"""Перевод GSSError в ошибки kerberos-слоя (база и виды — boba.kerberos).

Ошибки: своих не выпускает; классифицирует чужие.
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

from boba.kerberos import (
    CredentialsExpiredError,
    DelegationNotPermittedError,
    KerberosError,
    KeytabError,
)

__all__ = ["GssErrors"]


class GssErrors:
    """Перевод GSSError в ошибки слоя."""

    @staticmethod
    def of(exc: GSSError, context: str) -> KerberosError:
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
