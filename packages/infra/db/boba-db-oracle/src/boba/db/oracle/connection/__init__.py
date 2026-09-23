"""Профиль соединения Oracle: параметры thin-соединения, границы сессии, auth."""

from boba.db.oracle.connection.auth import (
    NetProtocol,
    OracleAuth,
    OracleAuthBase,
    OracleAuthMethod,
    PasswordAuth,
    WalletAuth,
)
from boba.db.oracle.connection.config import OracleConfig

__all__ = [
    "NetProtocol",
    "OracleAuth",
    "OracleAuthBase",
    "OracleAuthMethod",
    "OracleConfig",
    "PasswordAuth",
    "WalletAuth",
]
