"""Boba extension: read-only ChromaDB knowledge-base tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.config.app import ConfigError
from boba.ext.chromadb.config import ChromadbSection
from boba.ext.chromadb.indexer_store import ChromadbPersistStoreFactory
from boba.ext.chromadb.kb import get_knowledge_base
from boba.ext.chromadb.kb_list_collections import (
    KbListCollectionsTool,
    KbListCollectionsToolSection,
)
from boba.ext.chromadb.kb_search import KbSearchTool, KbSearchToolSection
from boba.indexing import IndexerExtensionContext, StoreFactory
from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

__all__ = ["register_stores", "register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: chromadb-tools, гейт по [ext.chromadb] enable."""
    cfg = ctx.config.section(ChromadbSection)
    if not cfg.enable:
        return
    if not cfg.persist_path:
        raise ConfigError("[ext.chromadb] persist_path is required when enable=true")
    kb = get_knowledge_base(cfg)
    s = ctx.config.section
    tools = [
        KbListCollectionsTool(kb, s(KbListCollectionsToolSection)),
        KbSearchTool(kb, s(KbSearchToolSection)),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("ext.chromadb"),
        priority=0,
        tools=tools,
    )


def register_stores(
    ctx: IndexerExtensionContext,
) -> Iterable[StoreFactory]:
    """Entry-point boba.indexing.stores: ChromadbPersistStore."""
    del ctx
    yield ChromadbPersistStoreFactory()
