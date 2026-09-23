"""Профиль соединения Oracle: параметры thin-соединения, границы сессии, auth."""

from boba.db.oracle.connection.auth import OracleAuth, OracleAuthBase, PasswordAuth
from boba.db.oracle.connection.config import OracleConfig

__all__ = [
    "OracleAuth",
    "OracleAuthBase",
    "OracleConfig",
    "PasswordAuth",
]
