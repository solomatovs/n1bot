"""Манифест плагина pg: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.whitelist import ConnectionKeying
from boba.db.postgres.connection import MANIFEST as PG_CONNECTION
from boba.tool.pg.tools import TOOLS

MANIFEST: Final = ConnectedToolManifest(
    section="pg",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(PG_CONNECTION.kind, ConnectionKeying.NAME),
)
