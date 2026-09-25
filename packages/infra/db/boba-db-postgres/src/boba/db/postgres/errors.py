"""Ошибки инфраструктуры postgres."""

from __future__ import annotations

__all__ = ["PgArrowError", "PostgresError"]


class PostgresError(Exception):
    """Базовая ошибка PG-инфры: пул, соединение, таймаут, авторизация."""


class PgArrowError(RuntimeError):
    """Выборка для потока Arrow не описывается сервером или её колонка не
    укладывается в тип Arrow (numeric без точности)."""
