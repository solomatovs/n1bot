"""Имена объектов стенда насосов на общих серверах: схемы postgres, базы
ClickHouse, пользователь Oracle.

Под pytest-xdist тесты идут несколькими процессами против одних и тех же
серверов. Каждый процесс получает свою метку в именах, поэтому процессы не
пересоздают и не сносят данные друг друга. Метку ставит conftest стенда в
pytest_configure — раньше импорта модулей тестов, где имена подставляются в
SQL. Без xdist метки нет и имена прежние.

Ошибок наружу нет.
"""

from __future__ import annotations

from typing import ClassVar

__all__ = ["StandNames"]


class StandNames:
    """Имя объекта стенда с меткой рабочего процесса xdist: shop_ch_gw3."""

    SEPARATOR: ClassVar[str] = "_"

    _worker: ClassVar[str] = ""

    def use_worker(self, worker: str) -> None:
        StandNames._worker = worker

    def of(self, name: str) -> str:
        if not StandNames._worker:
            return name

        return f"{name}{self.SEPARATOR}{StandNames._worker}"
