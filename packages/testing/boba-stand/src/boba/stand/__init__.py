"""Общий стенд тестов: конфиг приложения, база и пул, kerberos, контекст вызова.

Фикстуры подключаются плагином `boba.stand.fixtures` из корневого conftest; помощники
контекста вызова — из `boba.stand_core.context`.
"""

from boba.stand.database import TestDatabase
from boba.stand_core.context import (
    TEST_PROFILE,
    TEST_TURN,
    install_context,
    make_context,
    use_context,
)

__all__ = [
    "TEST_PROFILE",
    "TEST_TURN",
    "TestDatabase",
    "install_context",
    "make_context",
    "use_context",
]
