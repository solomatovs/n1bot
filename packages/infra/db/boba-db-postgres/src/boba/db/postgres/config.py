"""PostgresConfig: DSN + параметры pool'а и таймаутов."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PostgresConfig"]


@dataclass(frozen=True)
class PostgresConfig:
    """DSN libpq + параметры pool'а; read-only-режим декларируется в DSN.

    dsn — libpq-строка вида postgresql://user:pass@host:5432/db?options=....
    Чтобы pool работал в read-only, задайте параметры прямо в DSN:
    ?default_transaction_read_only=on&statement_timeout=5000.
    Pool сам ничего read-only не выставляет — это контракт DSN.
    """

    dsn: str
    min_size: int = 1
    max_size: int = 4
    connect_timeout_sec: float = 10.0
