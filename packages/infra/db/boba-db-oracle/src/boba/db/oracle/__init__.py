"""boba.db.oracle — клиент Oracle; конфиг — boba.db.oracle.connection.

Клиент (boba.db.oracle.payload) отсюда не экспортируется: он тянет python-oracledb,
которого в окружении приложения нет — драйвер объявлен extra `payload`.
"""

from __future__ import annotations

from boba.db.oracle.errors import OracleError, OracleFormatError, OracleQueryError
from boba.db.oracle.query import (
    OraBindMarks,
    OraIdentifier,
    OraIdentifiers,
    OraLiterals,
    OraPiece,
    OraQuery,
    OraQueryBuilder,
)

__all__ = [
    "OraBindMarks",
    "OraIdentifier",
    "OraIdentifiers",
    "OraLiterals",
    "OraPiece",
    "OraQuery",
    "OraQueryBuilder",
    "OracleError",
    "OracleFormatError",
    "OracleQueryError",
]
