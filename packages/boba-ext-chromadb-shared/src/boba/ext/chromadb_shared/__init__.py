"""Shared ChromaDB config: persist_path + embedding (model/base_url/api_key).

`embedding_model = "default"` — встроенный ONNX `all-MiniLM-L6-v2` ChromaDB
(скачивается локально, без сети). Любое другое имя — внешний OpenAI-
совместимый embeddings endpoint (LiteLLM, Ollama, OpenAI). В этом случае
`embedding_base_url` и `embedding_api_key` обязательны.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from boba.coercion import ChainCoercer, Default, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ChromadbSharedConfig",
    "ChromadbSharedSection",
    "make_embedding_function",
]


@dataclass(frozen=True)
class ChromadbSharedConfig:
    """Общие настройки ChromaDB-бэкенда."""

    persist_path: str = ""
    embedding_model: str = "default"
    embedding_base_url: str = ""
    embedding_api_key: str = ""


class ChromadbSharedSection(ConfigSection[ChromadbSharedConfig]):
    """Секция [ext.chromadb] — общие настройки tools и store."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[ChromadbSharedConfig]] = ObjectSchema(
        description=(
            "Общая конфигурация ChromaDB: persist_path + embedding-параметры "
            "(model/base_url/api_key). Делится между tools (read) и store (write)."
        ),
        fields=[
            FieldSpec(
                name="persist_path",
                coercer=ChainCoercer(Default(""), ParseString()),
                description="Путь к persistent ChromaDB.",
            ),
            FieldSpec(
                name="embedding_model",
                coercer=ChainCoercer(Default("default"), ParseString()),
                description=(
                    "Имя embedding-модели. 'default' = built-in ONNX "
                    "all-MiniLM-L6-v2; иначе — модель LiteLLM/OpenAI-API "
                    "(например 'ollama/nomic-embed-text', "
                    "'text-embedding-3-small')."
                ),
            ),
            FieldSpec(
                name="embedding_base_url",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "OpenAI-совместимый endpoint embeddings "
                    "(LiteLLM/Ollama). Игнорируется при model=default."
                ),
            ),
            FieldSpec(
                name="embedding_api_key",
                coercer=ChainCoercer(Default(""), ParseString()),
                description="API key embeddings endpoint'а (можно пусто для Ollama).",
            ),
        ],
        factory=ChromadbSharedConfig,
    )


def make_embedding_function(cfg: ChromadbSharedConfig) -> Any:
    """Построить chromadb embedding_function из конфига; None = built-in default."""
    if cfg.embedding_model in ("", "default"):
        return None
    # Ленивый импорт chromadb — не падать при импорте пакета без deps
    from chromadb.utils.embedding_functions import (  # noqa: PLC0415
        OpenAIEmbeddingFunction,
    )

    return OpenAIEmbeddingFunction(
        api_key=cfg.embedding_api_key or "unused",
        api_base=cfg.embedding_base_url or None,
        model_name=cfg.embedding_model,
    )
