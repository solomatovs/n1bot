"""Манифест плагина ch: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.profile import ConnectionKind
from boba.connections.whitelist import ConnectionKeying
from boba.tool.ch.tools import TOOLS

MANIFEST: Final = ConnectedToolManifest(
    section="ch",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(ConnectionKind.CLICKHOUSE, ConnectionKeying.NAME),
)
