"""boba.db.clickhouse — конфиг подключения к ClickHouse.

Клиент (boba.db.clickhouse.payload) отсюда не экспортируется: он тянет
clickhouse-connect, которого в окружении приложения нет — драйвер объявлен
extra `payload` и ставится только в песочницу.
"""

from __future__ import annotations

from boba.db.clickhouse.auth import (
    CertificateAuth,
    ClickHouseAuth,
    ClickHouseAuthError,
    ClickHouseAuthMethod,
    NoPasswordAuth,
    PasswordAuth,
)
from boba.db.clickhouse.config import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.errors import ClickHouseError

__all__ = [
    "CertificateAuth",
    "ClickHouseAuth",
    "ClickHouseAuthError",
    "ClickHouseAuthMethod",
    "ClickHouseConfig",
    "ClickHouseError",
    "ClickHouseSettingsConfig",
    "NoPasswordAuth",
    "PasswordAuth",
]
