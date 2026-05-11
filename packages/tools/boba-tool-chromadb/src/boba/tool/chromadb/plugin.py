"""ChromadbPlugin: единая точка регистрации ChromaDB read-tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay, prompt_field
from boba.schema.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    ParseInt,
    ParseString,
    Required,
)
from boba.schema.declaration import FieldSpec, ObjectSchema
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
    """Плоский DTO плагина: persist+embedding + per-tool params + prompts."""

    persist_path: str
    embedding_model: str
    embedding_base_url: str
    embedding_api_key: str
    snippet_chars: int
    max_top_k: int
    kb_search: PromptOverlay
    kb_list_collections: PromptOverlay


class ChromadbPlugin(Plugin[ChromadbPluginConfig, ToolSource]):
    """Plugin ChromaDB read-tools: kb_search + kb_list_collections."""

    NAME: ClassVar[StrId] = StrId("chromadb")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.chromadb")

    @classmethod
    def config(cls) -> ObjectSchema[ChromadbPluginConfig]:
        return ObjectSchema(
            description=(
                "ChromaDB read-tools: kb_search + kb_list_collections. "
                "persist_path обязателен; embedding_model='default' = "
                "built-in ONNX (без сети)."
            ),
            fields=[
                FieldSpec(
                    name="persist_path",
                    coercer=ChainCoercer(Required(), ParseString()),
                    description="Путь к persistent ChromaDB.",
                ),
                FieldSpec(
                    name="embedding_model",
                    coercer=ChainCoercer(Default("default"), ParseString()),
                    description=(
                        "'default' = built-in ONNX all-MiniLM-L6-v2; иначе — "
                        "модель LiteLLM/OpenAI-API."
                    ),
                ),
                FieldSpec(
                    name="embedding_base_url",
                    coercer=ChainCoercer(Default(""), ParseString()),
                    description=(
                        "OpenAI-совместимый endpoint embeddings. "
                        "Игнорируется при model=default."
                    ),
                ),
                FieldSpec(
                    name="embedding_api_key",
                    coercer=ChainCoercer(Default(""), ParseString()),
                    description="API key embeddings endpoint'а.",
                ),
                FieldSpec(
                    name="snippet_chars",
                    coercer=ChainCoercer(Default(300), ParseInt(), MinValue(1)),
                    description="Максимальная длина сниппета документа в kb_search.",
                ),
                FieldSpec(
                    name="max_top_k",
                    coercer=ChainCoercer(Default(20), ParseInt(), MinValue(1)),
                    description="Жёсткий потолок параметра top_k.",
                ),
                prompt_field("kb_search"),
                prompt_field("kb_list_collections"),
            ],
            factory=ChromadbPluginConfig,
        )

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
                        max_top_k=cfg.max_top_k, prompt=cfg.kb_search,
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
        # Lazy import — избегаем падения при импорте без chromadb deps.
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            OpenAIEmbeddingFunction,
        )

        return OpenAIEmbeddingFunction(
            api_key=cfg.embedding_api_key or "unused",
            api_base=cfg.embedding_base_url or None,
            model_name=cfg.embedding_model,
        )
