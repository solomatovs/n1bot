"""boba.db.clickhouse — клиент ClickHouse; конфиг — boba.connections.clickhouse.

Клиент (boba.db.clickhouse.payload) отсюда не экспортируется: он тянет
clickhouse-connect, которого в окружении приложения нет — драйвер объявлен
extra `payload` и ставится только в песочницу.
"""

from __future__ import annotations

from boba.db.clickhouse.errors import ClickHouseError

__all__ = [
    "ClickHouseError",
]
