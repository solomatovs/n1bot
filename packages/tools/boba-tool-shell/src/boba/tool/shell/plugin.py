"""Манифест bash-плагина: entry point группы boba.tools."""

from typing import Final

from boba.tool.shell.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="bash", tools=TOOLS)
