"""Объявление плагина describer: entry point группы boba.tools."""

from typing import Final

from boba.tool.describer.store import DescriberStore, DescriberToolConfig
from boba.tool.describer.tools import TOOLS
from boba.toolkit.manifest import PluginSetup, ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="describer",
    tools=tuple(TOOLS),
    setup=PluginSetup(DescriberToolConfig, DescriberStore),
    network=True,
)
