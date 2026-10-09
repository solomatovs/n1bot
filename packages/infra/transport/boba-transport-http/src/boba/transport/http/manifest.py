"""Тип соединения web: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — хук получил профиль чужого типа.
httpx.HTTPError — пробный запрос не прошёл.
"""

from __future__ import annotations

from boba.connections.base import ConnectionBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.transport.http.connection import HttpConnection
from boba.transport.http.transport import (
    HttpRequest,
    HttpTransport,
    HttpTransportConfig,
)

__all__ = ["MANIFEST"]


async def _probe(connection: ConnectionBase) -> str:
    """Проба по контракту ProbeHook: профиль реестра соединений — web-соединение."""
    if not isinstance(connection, HttpConnection):
        msg = f"web probe expects an HttpConnection, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    async with (
        HttpTransport(connection, HttpTransportConfig()) as transport,
        transport.fetch(HttpRequest(url=str(connection.root_url()))) as got,
    ):
        await got.stream.read()
        return f"HTTP {got.status}"


def _address(connection: ConnectionBase) -> str:
    """Адрес по контракту AddressHook: корень сервиса без учётных данных."""
    if not isinstance(connection, HttpConnection):
        msg = f"web address expects an HttpConnection, got kind {connection.kind!r}"
        raise ConnectionTypeError(msg)

    return str(connection.public_url())


MANIFEST = ConnectionTypeManifest(
    kind="web",
    model=HttpConnection,
    probe=_probe,
    address=_address,
)
