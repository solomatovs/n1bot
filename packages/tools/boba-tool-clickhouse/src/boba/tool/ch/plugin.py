"""Манифест плагина ch: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.whitelist import ConnectionKeying
from boba.db.clickhouse.connection import MANIFEST as CH_CONNECTION
from boba.tool.ch.tools import TOOLS

MANIFEST: Final = ConnectedToolManifest(
    section="ch",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(CH_CONNECTION.kind, ConnectionKeying.NAME),
)
