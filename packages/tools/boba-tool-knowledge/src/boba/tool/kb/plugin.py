"""Манифест плагина kb: entry point группы boba.tools."""

from typing import Final

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.schema import KbSchema
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.toolkit.manifest import PluginStart, ToolPluginManifest

KB: Final = ToolPluginManifest(
    section="kb",
    tools=tuple(KB_TOOLS),
    start=PluginStart(KnowledgeBaseSchemaConfig, KbSchema),
)
