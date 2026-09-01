"""Манифест плагина web: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.whitelist import ConnectionKeying
from boba.tool.web.tools import TOOLS
from boba.transport.http.connection import MANIFEST as WEB_CONNECTION

MANIFEST: Final = ConnectedToolManifest(
    section="web",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(WEB_CONNECTION.kind, ConnectionKeying.NAME),
)
