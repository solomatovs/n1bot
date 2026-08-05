"""Ошибки подключения к ClickHouse."""

from __future__ import annotations

__all__ = ["ClickHouseError"]


class ClickHouseError(RuntimeError):
    """До ClickHouse не достучаться: сеть, TLS, kerberos, отказ HTTP-клиента."""
