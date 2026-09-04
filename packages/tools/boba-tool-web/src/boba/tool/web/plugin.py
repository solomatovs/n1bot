"""Манифест плагина web: entry point группы boba.tools."""

from typing import Final

from boba.tool.web.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="web", tools=tuple(TOOLS))
