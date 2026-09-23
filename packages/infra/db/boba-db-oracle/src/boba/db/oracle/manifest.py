"""Тип соединения oracle: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — probe-хук получил профиль чужого типа.
OracleError — пробное соединение не открылось.
OracleQueryError — сервер отклонил пробный запрос.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.oracle.connection import OracleConfig

__all__ = ["MANIFEST"]

PROBE_SQL = "select banner from v$version where rownum = 1"
"""v$version открыт любой учётке, грантов не требует."""


async def _probe(connection: ConnectionBase) -> str:
    if not isinstance(connection, OracleConfig):
        msg = f"oracle probe expects an OracleConfig, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    # клиент тянет python-oracledb и pyarrow: в окружении приложения их нет,
    # а манифест читается там при разборе конфига соединений
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415

    payload = PayloadOracle(connection)
    banner = ""
    async with payload.opened() as conn, payload.rows(conn, PROBE_SQL) as stream:
        async for row in stream.blocks:
            banner = str(row[0])
            break

    if not banner:
        return "connected"

    return banner


MANIFEST = ConnectionTypeManifest(
    kind=OracleConfig.KIND,
    model=OracleConfig,
    probe=_probe,
)
