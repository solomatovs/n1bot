"""Исключения boba-db-postgres."""

from __future__ import annotations

__all__ = ["PostgresError", "PostgresPoolClosedError"]


class PostgresError(Exception):
    """Базовая ошибка PG-инфры (pool/connection/timeout)."""


class PostgresPoolClosedError(PostgresError):
    """Попытка взять connection из уже закрытого pool'а."""
