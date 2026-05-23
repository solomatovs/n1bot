"""`build_embedder` — factory `Embedder[str]` из `EmbeddingModel`-конфига.

Бэкенд выбирается по `EmbeddingModel.provider`:
  - "openai-compat" — `OpenAICompatEmbedder` поверх OpenAI-SDK
    (LiteLLM / OpenAI / vLLM / Ollama). Без `dimensions=` параметра,
    размерность — через lazy probe в `dim()`.
  - "local" — `LocalFastEmbedEmbedder` (fastembed/ONNX, in-process,
    без сети).
"""

from __future__ import annotations

from openai import OpenAI

from boba.indexing.embedder import Embedder
from boba.provider.openai import OpenAICompatEmbedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.local_embedder import LocalFastEmbedEmbedder

__all__ = ["build_embedder"]


def build_embedder(model: EmbeddingModel) -> Embedder[str]:
    if model.provider == "local":
        return LocalFastEmbedEmbedder(
            model_name=model.model,
            cache_dir=model.cache_dir or None,
        )

    client = OpenAI(
        base_url=model.endpoint or None,
        api_key=model.api_key or "unused",
    )
    return OpenAICompatEmbedder(
        client=client,
        model=model.model,
    )
