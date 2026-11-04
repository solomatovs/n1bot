"""Граница ошибок хранилища личностей: конфликт, дефект данных, доступность."""

from typing import ClassVar

import psycopg

from boba.db.postgres import PostgresTable
from boba.db.postgres.errors import PostgresConcurrentChangeError
from boba.identity.errors import (
    ExternalServiceError,
    IdentityConflictError,
    InternalServiceError,
)


class IdentityTable(PostgresTable):
    LABEL: ClassVar[str] = "identity"

    def _unavailable(self, action: str, exc: Exception) -> Exception:
        return ExternalServiceError("identity", self._detail(action, exc))

    def _broken(self, action: str, exc: Exception) -> Exception:
        return InternalServiceError(
            self._detail(action, exc), "Invalid identity storage state"
        )

    def _conflict(self, action: str, detail: str) -> Exception:
        return IdentityConflictError(f"{action}: {detail}")

    def _failure(self, action: str, exc: Exception) -> Exception:
        if isinstance(exc, PostgresConcurrentChangeError):
            return self._conflict(action, str(exc))
        if isinstance(
            exc, psycopg.errors.UniqueViolation
        ) and exc.diag.constraint_name in {
            "users_realm_login_key",
            "users_identifier_key",
            "idx_users_identifier_lower",
        }:
            return self._conflict(
                action, "the requested identity already belongs to another user"
            )
        if isinstance(exc, psycopg.errors.ForeignKeyViolation):
            return self._conflict(
                action,
                f"related data prevents the change ({exc.diag.constraint_name})",
            )
        if isinstance(
            exc,
            (
                psycopg.ProgrammingError,
                psycopg.IntegrityError,
                psycopg.DataError,
                psycopg.InterfaceError,
            ),
        ):
            return self._broken(action, exc)
        return self._unavailable(action, exc)
