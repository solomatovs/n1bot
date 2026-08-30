"""Общий стенд тестов: конфиг приложения, тестовая база и пул, kerberos, контекст вызова.

Фикстуры подключаются плагином `boba.stand.fixtures` из корневого conftest; помощники
контекста вызова — из `boba.stand.context`.
"""

from boba.stand.context import (
    TEST_PROFILE,
    TEST_TURN,
    install_context,
    make_context,
    use_context,
)
from boba.stand.database import TestDatabase

__all__ = [
    "TEST_PROFILE",
    "TEST_TURN",
    "TestDatabase",
    "install_context",
    "make_context",
    "use_context",
]
