"""Манифест плагина chart: entry point группы boba.tools."""

from typing import Final

from boba.tool.chart.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(
    section="chart",
    tools=tuple(TOOLS),
)
