"""Тип соединения clickhouse: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — probe-хук получил профиль чужого типа.
ClickHouseError — пробное соединение не открылось или запрос не прошёл.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.snapshot import ChSourceKind

__all__ = ["MANIFEST"]

PROBE_SQL = "select version()"


async def _probe(connection: ConnectionBase) -> str:
    if not isinstance(connection, ClickHouseConfig):
        msg = (
            f"clickhouse probe expects a ClickHouseConfig, got kind {connection.kind!r}"
        )
        raise ConnectionTypeError(msg)

    # клиент тянет clickhouse-connect: в окружении приложения его нет,
    # а манифест читается там при разборе конфига соединений
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415

    async with PayloadClickHouse.opened_config(connection) as client:
        result = await client.query(PROBE_SQL)

    rows = result.result_rows
    if not rows:
        return "connected"

    first = rows[0]
    if isinstance(first, tuple | list) and first:
        return str(first[0])

    return "connected"


MANIFEST = ConnectionTypeManifest(
    kind=ChSourceKind.CLICKHOUSE.value,
    model=ClickHouseConfig,
    probe=_probe,
)
