"""Тип соединения postgres: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
AddressError — у профиля нет ни host, ни hostaddr: адреса не собрать.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.postgres.address import PgAddresses
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.snapshot import PgSourceKind

__all__ = ["MANIFEST"]


def _address(connection: ConnectionBase) -> str:
    if not isinstance(connection, PostgresConfig):
        msg = f"postgres address expects a PostgresConfig, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    return PgAddresses.url_of(connection)


MANIFEST = ConnectionTypeManifest(
    kind=PgSourceKind.POSTGRES.value,
    model=PostgresConfig,
    address=_address,
)
