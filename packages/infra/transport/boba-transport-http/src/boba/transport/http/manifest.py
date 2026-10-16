"""Тип соединения web: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.transport.http.connection import HttpConnection

__all__ = ["MANIFEST"]


def _address(connection: ConnectionBase) -> str:
    """Адрес по контракту AddressHook: корень сервиса без учётных данных."""
    if not isinstance(connection, HttpConnection):
        msg = f"web address expects an HttpConnection, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    return str(connection.public_url())


MANIFEST = ConnectionTypeManifest(
    kind="web",
    model=HttpConnection,
    address=_address,
)
