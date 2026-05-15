"""ChromadbPlugin: единая точка регистрации ChromaDB read-tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MinValue, ParseInt, ParseString
from boba.tool.chromadb.kb import get_knowledge_base
from boba.tool.chromadb.kb_list_collections import (
    KbListCollectionsTool,
    KbListCollectionsToolConfig,
)
from boba.tool.chromadb.kb_search import KbSearchTool, KbSearchToolConfig
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ChromadbPlugin", "ChromadbPluginConfig"]


@dataclass(frozen=True)
class ChromadbPluginConfig:
    """
    ChromaDB read-tools: kb_search + kb_list_collections. persist_path обязателен;
    embedding_model='default' = built-in ONNX (без сети)
    """

    persist_path: Annotated[str, "Путь к persistent ChromaDB.", ParseString()]
    embedding_model: Annotated[
        str,
        "'default' = built-in ONNX all-MiniLM-L6-v2" \
        "иначе — модель LiteLLM/OpenAI-API.",
        ParseString(),
    ] = "default"
    embedding_base_url: Annotated[
        str,
        "OpenAI-совместимый endpoint embeddings. Игнорируется при model=default.",
        ParseString(),
    ] = ""
    embedding_api_key: Annotated[
        str,
        "API key embeddings endpoint'а.",
        ParseString(),
    ] = ""
    snippet_chars: Annotated[
        int,
        "Максимальная длина сниппета документа в kb_search.",
        ParseInt(),
        MinValue(1),
    ] = 300
    max_top_k: Annotated[
        int,
        "Жёсткий потолок параметра top_k.",
        ParseInt(),
        MinValue(1),
    ] = 20
    kb_search: PromptOverlay = field(default_factory=PromptOverlay)
    kb_list_collections: PromptOverlay = field(default_factory=PromptOverlay)


class ChromadbPlugin(Plugin[ChromadbPluginConfig, ToolSource]):
    """Plugin ChromaDB read-tools: kb_search + kb_list_collections."""

    NAME: ClassVar[str] = "chromadb"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.chromadb")

    @classmethod
    def build(
        cls,
        cfg: ChromadbPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        kb = get_knowledge_base(
            cfg.persist_path,
            cfg.snippet_chars,
            embedding_function=cls._embedding_function(cfg),
        )
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                KbSearchTool(
                    kb,
                    KbSearchToolConfig(
                        max_top_k=cfg.max_top_k,
                        prompt=cfg.kb_search,
                    ),
                    ctx,
                    sid,
                ),
                KbListCollectionsTool(
                    kb,
                    KbListCollectionsToolConfig(prompt=cfg.kb_list_collections),
                    ctx,
                    sid,
                ),
            ],
        )

    @staticmethod
    def _embedding_function(cfg: ChromadbPluginConfig) -> Any:
        """chromadb embedding_function из конфига; None = built-in default."""
        if cfg.embedding_model in ("", "default"):
            return None
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            OpenAIEmbeddingFunction,
        )

        return OpenAIEmbeddingFunction(
            api_key=cfg.embedding_api_key or "unused",
            api_base=cfg.embedding_base_url or None,
            model_name=cfg.embedding_model,
        )
