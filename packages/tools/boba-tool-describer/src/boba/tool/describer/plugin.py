"""Манифест плагина describer: entry point группы boba.tools."""

from typing import Final

from boba.tool.describer.store import DescriberStore, DescriberToolConfig
from boba.tool.describer.tools import TOOLS
from boba.toolkit.manifest import PluginStart, ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(
    section="describer",
    tools=tuple(TOOLS),
    start=PluginStart(DescriberToolConfig, DescriberStore),
)
