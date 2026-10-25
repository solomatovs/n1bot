"""Объявление плагина chart: entry point группы boba.tools."""

from typing import Final

from boba.tool.chart.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="chart",
    tools=tuple(TOOLS),
)
