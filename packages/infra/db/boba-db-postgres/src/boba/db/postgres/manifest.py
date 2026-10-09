"""Тип соединения postgres: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
PostgresError — пробное соединение не открылось или запрос не прошёл.
AddressError — у профиля нет ни host, ни hostaddr: адреса не собрать.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.postgres.address import PgAddresses
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.payload import PayloadPostgres
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.snapshot import PgSourceKind

__all__ = ["MANIFEST"]


async def _probe(connection: ConnectionBase) -> str:
    if not isinstance(connection, PostgresConfig):
        msg = f"postgres probe expects a PostgresConfig, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    probe = PgQueryBuilder().add("select version()").build()
    conn = await PayloadPostgres.connect_config(connection)
    try:
        async with conn.cursor() as cur:
            await cur.execute(probe.text, probe.params)
            row = await cur.fetchone()
    finally:
        await conn.close()

    if isinstance(row, tuple | list) and row:
        return str(row[0])

    return "connected"


def _address(connection: ConnectionBase) -> str:
    if not isinstance(connection, PostgresConfig):
        msg = f"postgres address expects a PostgresConfig, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    return PgAddresses.url_of(connection)


MANIFEST = ConnectionTypeManifest(
    kind=PgSourceKind.POSTGRES.value,
    model=PostgresConfig,
    probe=_probe,
    address=_address,
)
