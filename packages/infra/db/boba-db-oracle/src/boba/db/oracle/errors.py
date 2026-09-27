"""Ошибки подключения к Oracle."""

from __future__ import annotations

__all__ = ["OracleError", "OracleMissingObjectError", "OracleQueryError"]


class OracleError(RuntimeError):
    """До Oracle не достучаться: сеть, listener, отказ сервера при входе."""


class OracleQueryError(RuntimeError):
    """Сервер отклонил запрос или оборвал чтение: синтаксис, права, таймаут вызова.

    Отдельно от OracleError: до базы достучались, дело в самом запросе. Ошибка
    драйвера сюда упаковывается на границе пакета, наружу тип python-oracledb не
    выходит — в окружении приложения его нет.
    """


class OracleMissingObjectError(OracleQueryError):
    """Объекта, который назвал стейтмент, нет или он не виден учётке
    (ORA-00942): приёмник по нему узнаёт, что таблицы ещё нет."""
