"""boba.db.oracle — клиент Oracle; конфиг — boba.db.oracle.profile.

Клиент (boba.db.oracle.payload) отсюда не экспортируется: он тянет python-oracledb,
которого в окружении приложения нет — драйвер объявлен extra `payload`.
"""

from __future__ import annotations

from boba.db.oracle.errors import OracleError, OracleQueryError

__all__ = [
    "OracleError",
    "OracleQueryError",
]
