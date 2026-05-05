"""Boba extension: read-only ChromaDB knowledge-base tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.config.app import ConfigError
from boba.ext.chromadb_shared import (
    ChromadbSharedSection,
    make_embedding_function,
)
from boba.ext.chromadb_tools.config import ChromadbToolsSection
from boba.ext.chromadb_tools.kb import get_knowledge_base
from boba.ext.chromadb_tools.kb_list_collections import (
    KbListCollectionsTool,
    KbListCollectionsToolSection,
)
from boba.ext.chromadb_tools.kb_search import KbSearchTool, KbSearchToolSection
from boba.tools.domain import ToolSourceId
from boba.tools.framework import (
    ExtensionContext,
    StaticToolSource,
    ToolSource,
)

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: chromadb-tools, гейт по [ext.chromadb.tools] enable."""
    tools_cfg = ctx.config.section(ChromadbToolsSection)
    if not tools_cfg.enable:
        return
    shared = ctx.config.section(ChromadbSharedSection)
    if not shared.persist_path:
        msg = (
            "[ext.chromadb] persist_path is required when "
            "[ext.chromadb.tools] enable=true"
        )
        raise ConfigError(msg)
    kb = get_knowledge_base(
        shared.persist_path,
        tools_cfg.snippet_chars,
        embedding_function=make_embedding_function(shared),
    )
    s = ctx.config.section
    tools = [
        KbListCollectionsTool(kb, s(KbListCollectionsToolSection)),
        KbSearchTool(kb, s(KbSearchToolSection)),
    ]
    if tools_cfg.tools_allow:
        allow = set(tools_cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("ext.chromadb_tools"),
        priority=0,
        tools=tools,
    )
