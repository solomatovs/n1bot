"""Манифест плагина kb: entry point группы boba.tools."""

from typing import Final

from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.toolkit.manifest import ToolPluginManifest

KB: Final = ToolPluginManifest(section="kb", tools=tuple(KB_TOOLS))
