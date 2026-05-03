"""Boba extension: read-only ChromaDB knowledge-base tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba_next.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

from boba.ext.chromadb.config import ChromadbSection
from boba.ext.chromadb.kb import get_knowledge_base
from boba.ext.chromadb.kb_list_collections import KbListCollectionsTool
from boba.ext.chromadb.kb_search import KbSearchTool

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: 2 read-only tools одним источником."""
    cfg = ctx.config.section(ChromadbSection)
    kb = get_knowledge_base(cfg)
    yield StaticToolSource(
        ToolSourceId("ext.chromadb"),
        priority=0,
        tools=[
            KbListCollectionsTool(kb),
            KbSearchTool(kb, cfg),
        ],
    )
