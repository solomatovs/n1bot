"""ChromadbPlugin: единая точка регистрации ChromaDB read-tools."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.chromadb.kb import get_knowledge_base
from boba.tool.chromadb.kb_list_collections import (
    KbListCollectionsTool,
    KbListCollectionsToolConfig,
)
from boba.tool.chromadb.kb_search import KbSearchTool, KbSearchToolConfig
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ChromadbPlugin", "ChromadbPluginConfig"]


class ChromadbPluginConfig(BobaFlatSettings):
    """ChromaDB read-tools: kb_search + kb_list_collections.

    `persist_path` обязателен при `enable=True`. `embedding_model='default'` —
    built-in ONNX (без сети).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__CHROMADB__",
        boba_toml_section="tool.chromadb",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
    )
    persist_path: str = Field(
        default="",
        description="Путь к persistent ChromaDB (обязателен при enable=True).",
    )
    embedding_model: str = Field(
        default="default",
        description=(
            "'default' = built-in ONNX all-MiniLM-L6-v2; "
            "иначе — модель LiteLLM/OpenAI-API."
        ),
    )
    embedding_base_url: str = Field(
        default="",
        description=(
            "OpenAI-совместимый endpoint embeddings. "
            "Игнорируется при model=default."
        ),
    )
    embedding_api_key: str = Field(
        default="",
        description="API key embeddings endpoint'а.",
    )
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета документа в kb_search.",
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k.",
    )
    kb_search: PromptOverlay = Field(default_factory=PromptOverlay)
    kb_list_collections: PromptOverlay = Field(default_factory=PromptOverlay)

    @model_validator(mode="after")
    def _check_persist_path_when_enabled(self) -> Self:
        if self.enable and not self.persist_path:
            msg = "persist_path обязателен при enable=True"
            raise ValueError(msg)
        return self


class ChromadbPlugin(Plugin[ChromadbPluginConfig, ToolSource]):
    """Plugin ChromaDB read-tools: kb_search + kb_list_collections."""

    NAME: ClassVar[str] = "chromadb"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin_chromadb")

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
