"""ChromadbPlugin: единая точка регистрации ChromaDB tools.

Все 3 tool'а конструируются по единому шаблону `Tool(cfg, ctx, source_id)` —
никаких дополнительных параметров. Внутренние зависимости (chromadb client,
ChromaKnowledgeBase, Embedder через Dishka) собираются внутри tool'ов
лениво, в `execute()`. Plugin.build делает только две вещи:
1. Маппит `ChromadbPluginConfig` → per-tool config DTO.
2. Инстанциирует tool'ы.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.plugin import ExtensionContext, Plugin
from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tool.chromadb.kb_ingest import KbIngestTool, KbIngestToolConfig
from boba.tool.chromadb.kb_list_collections import (
    KbListCollectionsTool,
    KbListCollectionsToolConfig,
)
from boba.tool.chromadb.kb_search import KbSearchTool, KbSearchToolConfig
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ChromadbPlugin", "ChromadbPluginConfig"]


class ChromadbPlugin(Plugin[ChromadbPluginConfig, ToolSource]):
    """Plugin ChromaDB: kb_search + kb_list_collections + kb_ingest."""

    NAME: ClassVar[str] = "chromadb"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin_chromadb")

    @classmethod
    def build(
        cls,
        cfg: ChromadbPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                KbSearchTool(
                    KbSearchToolConfig(
                        persist_path=cfg.persist_path,
                        snippet_chars=cfg.snippet_chars,
                        embedding_model=cfg.embedding_model,
                        embedding_base_url=cfg.embedding_base_url,
                        embedding_api_key=cfg.embedding_api_key,
                        max_top_k=cfg.max_top_k,
                        prompt=cfg.kb_search,
                    ),
                    ctx,
                    sid,
                ),
                KbListCollectionsTool(
                    KbListCollectionsToolConfig(
                        persist_path=cfg.persist_path,
                        snippet_chars=cfg.snippet_chars,
                        embedding_model=cfg.embedding_model,
                        embedding_base_url=cfg.embedding_base_url,
                        embedding_api_key=cfg.embedding_api_key,
                        prompt=cfg.kb_list_collections,
                    ),
                    ctx,
                    sid,
                ),
                KbIngestTool(
                    KbIngestToolConfig(
                        persist_path=cfg.persist_path,
                        embedding_model=cfg.embedding_model,
                        embedding_base_url=cfg.embedding_base_url,
                        embedding_api_key=cfg.embedding_api_key,
                        ingest_folder=cfg.ingest_folder,
                        ingest_collection=cfg.ingest_collection,
                        ingest_collection_description=(
                            cfg.ingest_collection_description
                        ),
                        prompt=cfg.kb_ingest,
                    ),
                    ctx,
                    sid,
                ),
            ],
        )
