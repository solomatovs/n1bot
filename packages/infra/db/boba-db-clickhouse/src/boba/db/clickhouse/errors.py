"""Ошибки подключения к ClickHouse."""

from __future__ import annotations

__all__ = ["ClickHouseError", "ClickHouseQueryError"]


class ClickHouseError(RuntimeError):
    """До ClickHouse не достучаться: сеть, TLS, kerberos, отказ HTTP-клиента."""


class ClickHouseQueryError(RuntimeError):
    """Сервер отклонил запрос: синтаксис, права, тип значения.

    Отдельно от ClickHouseError: до базы достучались, дело в самом запросе.
    Ошибка драйвера сюда упаковывается на границе пакета, наружу тип
    clickhouse-connect не выходит — в окружении приложения его нет.
    """
