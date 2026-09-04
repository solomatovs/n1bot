"""Тип соединения postgres: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — probe-хук получил профиль чужого типа.
PostgresError — пробное соединение не открылось или запрос не прошёл.
"""

from __future__ import annotations

from psycopg import sql

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.postgres.payload import PayloadPostgres
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import PgSourceKind

__all__ = ["MANIFEST"]

PROBE_SQL = sql.SQL("select version()")


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, PostgresConfig):
        msg = (
            "postgres probe expects a PostgresConfig profile, "
            f"got kind {profile.kind!r}"
        )
        raise ConnectionTypeError(msg)

    conn = await PayloadPostgres.connect_config(profile)
    try:
        async with conn.cursor() as cur:
            await cur.execute(PROBE_SQL)
            row = await cur.fetchone()
    finally:
        await conn.close()

    if isinstance(row, tuple | list) and row:
        return str(row[0])

    return "connected"


MANIFEST = ConnectionTypeManifest(
    kind=PgSourceKind.POSTGRES.value,
    profile=PostgresConfig,
    probe=_probe,
)
