"""Объявление плагина kb: entry point группы boba.tools."""

from typing import Final

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.schema import KbSchema
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.kb.tools import warm_embedder
from boba.toolkit.manifest import PluginSetup, ToolPlugin

KB: Final = ToolPlugin(
    id="kb",
    tools=tuple(KB_TOOLS),
    setup=PluginSetup(KnowledgeBaseSchemaConfig, KbSchema),
    warmups=(warm_embedder,),
    network=True,
)
