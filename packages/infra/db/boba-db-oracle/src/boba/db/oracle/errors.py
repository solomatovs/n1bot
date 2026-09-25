"""Ошибки подключения к Oracle."""

from __future__ import annotations

__all__ = ["OracleError", "OracleFormatError", "OracleQueryError"]


class OracleError(RuntimeError):
    """До Oracle не достучаться: сеть, listener, отказ сервера при входе."""


class OracleQueryError(RuntimeError):
    """Сервер отклонил запрос или оборвал чтение: синтаксис, права, таймаут вызова.

    Отдельно от OracleError: до базы достучались, дело в самом запросе. Ошибка
    драйвера сюда упаковывается на границе пакета, наружу тип python-oracledb не
    выходит — в окружении приложения его нет.
    """


class OracleFormatError(RuntimeError):
    """Входной поток не того формата: байты не читаются как поток Arrow IPC
    или оборвались посреди сообщения.

    Отдельно от OracleQueryError: до сервера дело не дошло, не годятся байты,
    которые пришли на загрузку.
    """
