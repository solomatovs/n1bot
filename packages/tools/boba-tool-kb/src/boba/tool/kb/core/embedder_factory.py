"""`build_embedder` — factory OpenAI-compat embedder из `EmbeddingModel`-конфига.

OpenAI-совместимый endpoint (LiteLLM / OpenAI / vLLM / Ollama) через
`OpenAICompatEmbedder` — без `dimensions=` параметра, чтобы работал с Ollama.
Размерность модели определяется lazy probe'ом (см. `OpenAICompatEmbedder.dim()`).
"""

from __future__ import annotations

from openai import OpenAI

from boba.indexing.embedder import Embedder
from boba.provider.openai import OpenAICompatEmbedder
from boba.tool.kb.core.embedding_model import EmbeddingModel

__all__ = ["build_embedder"]


def build_embedder(model: EmbeddingModel) -> Embedder[str]:
    """Построить `Embedder[str]` из `EmbeddingModel`-конфига.

    `endpoint` передаётся в `OpenAI(base_url=...)` (внутри SDK это всё ещё
    base_url, но в нашем конфиге называется endpoint, чтобы не конфликтовать
    с другими `base_url`-полями при встраивании в плоский tool-конфиг).
    """
    client = OpenAI(
        base_url=model.endpoint or None,
        api_key=model.api_key or "unused",
    )
    return OpenAICompatEmbedder(
        client=client,
        model=model.model,
    )
