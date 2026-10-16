"""Манифесты плагинов Confluence: чтение (confluence) и индексация (ingest)."""

from typing import Final

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.schema import KbSchema
from boba.tool.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.toolkit.manifest import PluginStart, ToolPluginManifest

CONFLUENCE: Final = ToolPluginManifest(
    section="confluence", tools=tuple(CONFLUENCE_TOOLS)
)
INGEST: Final = ToolPluginManifest(
    section="ingest",
    tools=tuple(INGEST_TOOLS),
    start=PluginStart(KnowledgeBaseSchemaConfig, KbSchema),
)
