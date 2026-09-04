"""Манифест плагина pg: entry point группы boba.tools."""

from typing import Final

from boba.tool.pg.snapshot import TOOLS as SNAPSHOT_TOOLS
from boba.tool.pg.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="pg", tools=(*TOOLS, *SNAPSHOT_TOOLS))
