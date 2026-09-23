"""boba.db.oracle — клиент Oracle; конфиг — boba.db.oracle.connection.

Клиент (boba.db.oracle.payload) отсюда не экспортируется: он тянет python-oracledb,
которого в окружении приложения нет — драйвер объявлен extra `payload`.
"""

from __future__ import annotations

from boba.db.oracle.errors import OracleError, OracleQueryError
from boba.db.oracle.query import (
    OraIdentifier,
    OraQuery,
    OraQueryBuilder,
    OraSql,
)

__all__ = [
    "OraIdentifier",
    "OraQuery",
    "OraQueryBuilder",
    "OraSql",
    "OracleError",
    "OracleQueryError",
]
