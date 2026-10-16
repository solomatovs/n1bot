"""Тип соединения oracle: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.oracle.address import OraAddresses
from boba.db.oracle.connection import OracleConfig

__all__ = ["MANIFEST"]


def _address(connection: ConnectionBase) -> str:
    if not isinstance(connection, OracleConfig):
        msg = f"oracle address expects an OracleConfig, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    return OraAddresses.url_of(connection)


MANIFEST = ConnectionTypeManifest(
    kind=OracleConfig.KIND,
    model=OracleConfig,
    address=_address,
)
