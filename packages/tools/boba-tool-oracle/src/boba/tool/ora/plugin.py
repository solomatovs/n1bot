"""Манифест плагина ora: entry point группы boba.tools."""

from typing import Final

from boba.tool.ora.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="ora", tools=TOOLS)
