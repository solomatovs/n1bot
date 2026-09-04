"""Манифест плагина ch: entry point группы boba.tools."""

from typing import Final

from boba.tool.ch.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="ch", tools=tuple(TOOLS))
