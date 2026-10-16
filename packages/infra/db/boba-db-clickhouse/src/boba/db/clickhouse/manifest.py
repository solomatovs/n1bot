"""Тип соединения clickhouse: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
AddressError — у профиля нет host или port: адреса не собрать.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.snapshot import ChSourceKind

__all__ = ["MANIFEST"]


def _address(connection: ConnectionBase) -> str:
    if not isinstance(connection, ClickHouseConfig):
        msg = (
            "clickhouse address expects a ClickHouseConfig, "
            f"got kind {connection.kind!r}"
        )
        raise ConnectionTypeError(msg)

    return ChAddresses.url_of(connection)


MANIFEST = ConnectionTypeManifest(
    kind=ChSourceKind.CLICKHOUSE.value,
    model=ClickHouseConfig,
    address=_address,
)
