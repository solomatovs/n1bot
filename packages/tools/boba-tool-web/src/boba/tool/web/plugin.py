"""Манифест плагина web: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.profile import ConnectionKind
from boba.connections.whitelist import ConnectionKeying
from boba.tool.web.tools import TOOLS

MANIFEST: Final = ConnectedToolManifest(
    section="web",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(ConnectionKind.WEB, ConnectionKeying.NAME),
)
