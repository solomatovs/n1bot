"""Манифест плагина canvas: entry point группы boba.tools."""

from typing import Final

from boba.tool.canvas.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="canvas", tools=TOOLS)
