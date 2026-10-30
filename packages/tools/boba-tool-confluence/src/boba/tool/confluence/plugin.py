"""Объявления плагинов Confluence: чтение (confluence) и индексация (ingest)."""

from typing import Final

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.schema import KbSchema
from boba.tool.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.confluence.ingest_tools import warm_embedder
from boba.tool.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.toolkit.manifest import PluginSetup, ToolPlugin

CONFLUENCE: Final = ToolPlugin(
    id="confluence",
    tools=tuple(CONFLUENCE_TOOLS),
    network=True,
)
INGEST: Final = ToolPlugin(
    id="ingest",
    tools=tuple(INGEST_TOOLS),
    setup=PluginSetup(KnowledgeBaseSchemaConfig, KbSchema),
    warmups=(warm_embedder,),
    network=True,
)
