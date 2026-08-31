"""Манифесты плагинов базы знаний: kb, confluence и ingest одним пакетом."""

from typing import Final

from boba.tool.kb.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.kb.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.toolkit.manifest import ToolPluginManifest

KB: Final = ToolPluginManifest(section="kb", tools=tuple(KB_TOOLS))
CONFLUENCE: Final = ToolPluginManifest(
    section="confluence", tools=tuple(CONFLUENCE_TOOLS)
)
INGEST: Final = ToolPluginManifest(section="ingest", tools=tuple(INGEST_TOOLS))
