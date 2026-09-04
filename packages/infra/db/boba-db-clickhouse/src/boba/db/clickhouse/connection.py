"""Тип соединения clickhouse: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — probe-хук получил профиль чужого типа.
ClickHouseError — пробное соединение не открылось или запрос не прошёл.
"""

from __future__ import annotations

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.profile import ClickHouseConfig

__all__ = ["MANIFEST"]

PROBE_SQL = "select version()"


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, ClickHouseConfig):
        msg = (
            "clickhouse probe expects a ClickHouseConfig profile, "
            f"got kind {profile.kind!r}"
        )
        raise ConnectionTypeError(msg)

    async with PayloadClickHouse.opened_config(profile) as client:
        result = await client.query(PROBE_SQL)

    rows = result.result_rows
    if not rows:
        return "connected"

    first = rows[0]
    if isinstance(first, tuple | list) and first:
        return str(first[0])

    return "connected"


MANIFEST = ConnectionTypeManifest(
    kind="clickhouse",
    profile=ClickHouseConfig,
    probe=_probe,
)
