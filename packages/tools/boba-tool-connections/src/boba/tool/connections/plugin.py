"""Манифест плагина connections: entry point группы boba.tools."""

from typing import Final

from boba.tool.connections.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="connections", tools=TOOLS)
