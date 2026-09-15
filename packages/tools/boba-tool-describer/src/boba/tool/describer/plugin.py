"""Манифест плагина describer: entry point группы boba.tools."""

from typing import Final

from boba.tool.describer.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="describer", tools=tuple(TOOLS))
