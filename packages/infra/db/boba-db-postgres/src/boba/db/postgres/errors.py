"""Ошибки инфраструктуры postgres."""

from __future__ import annotations

__all__ = [
    "PgArrowError",
    "PgDescribeError",
    "PostgresConcurrentChangeError",
    "PostgresConnectionLostError",
    "PostgresError",
]


class PostgresError(Exception):
    """Базовая ошибка PG-инфры: пул, соединение, таймаут, авторизация."""


class PostgresConnectionLostError(PostgresError):
    """Соединение оборвалось посреди транзакции до её commit: сервер её не
    зафиксировал, и PostgresTable повторяет работу на новом соединении пула."""


class PostgresConcurrentChangeError(PostgresError):
    """Строка исчезла между командами; транзакцию можно повторить после отката."""


class PgDescribeError(RuntimeError):
    """Сервер отклонил стейтмент при описании его колонок (PQprepare или
    PQdescribePrepared) или колонка без имени в RowDescription."""


class PgArrowError(RuntimeError):
    """Выборка для потока Arrow не описывается сервером или её колонка не
    укладывается в тип Arrow (numeric без точности)."""
