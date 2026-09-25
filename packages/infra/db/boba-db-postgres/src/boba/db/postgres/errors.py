"""Ошибки инфраструктуры postgres."""

from __future__ import annotations

__all__ = ["PostgresError"]


class PostgresError(Exception):
    """Базовая ошибка PG-инфры: пул, соединение, таймаут, авторизация."""
