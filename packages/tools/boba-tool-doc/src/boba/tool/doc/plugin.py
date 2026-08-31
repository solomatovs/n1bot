"""Манифест плагина doc: entry point группы boba.tools."""

from typing import Final

from boba.tool.doc.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="doc", tools=tuple(TOOLS))
