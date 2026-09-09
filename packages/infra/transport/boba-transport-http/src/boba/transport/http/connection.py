"""Тип соединения web: манифест для реестра boba.connections.

Ошибки:
ConnectionTypeError — probe-хук получил профиль чужого типа.
httpx.HTTPError — пробный запрос не прошёл.
"""

from __future__ import annotations

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.transport.http.profile import HttpConnection
from boba.transport.http.transport import HttpRequest, HttpTransport

__all__ = ["MANIFEST"]


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, HttpConnection):
        msg = f"web probe expects an HttpConnection profile, got kind {profile.kind!r}"
        raise ConnectionTypeError(msg)

    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url=str(profile.root_url()))) as got,
    ):
        await got.stream.read()
        return f"HTTP {got.status}"


MANIFEST = ConnectionTypeManifest(
    kind="web",
    profile=HttpConnection,
    probe=_probe,
)
