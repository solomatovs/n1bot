"""Общий стенд тестов: конфиг приложения, база и пул, kerberos, контекст вызова.

Фикстуры подключаются плагином `boba.stand.fixtures` из корневого conftest; контекст
вызова теста — объект `CallStand` из `boba.stand_core.context`.
"""

from boba.stand.database import TestDatabase

__all__ = ["TestDatabase"]
