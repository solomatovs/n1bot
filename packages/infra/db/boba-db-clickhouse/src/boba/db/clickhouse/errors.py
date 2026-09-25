"""Ошибки подключения к ClickHouse."""

from __future__ import annotations

__all__ = ["ClickHouseError", "ClickHouseFormatError", "ClickHouseQueryError"]


class ClickHouseError(RuntimeError):
    """До ClickHouse не достучаться: сеть, TLS, kerberos, отказ HTTP-клиента."""


class ClickHouseQueryError(RuntimeError):
    """Сервер отклонил запрос: синтаксис, права, тип значения.

    Отдельно от ClickHouseError: до базы достучались, дело в самом запросе.
    Ошибка драйвера сюда упаковывается на границе пакета, наружу тип
    clickhouse-connect не выходит — в окружении приложения его нет.
    """


class ClickHouseFormatError(RuntimeError):
    """Поток не того формата: шапка с именами и типами не разбирается, поток
    оборвался до неё или в ней незнакомый тип.

    Отдельно от ClickHouseQueryError: сервер запрос принял, дело в байтах,
    которые пришли или которые собрались отправить.
    """
