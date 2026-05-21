"""Embedder factory для postgres KB-плагина.

Все embeddings идут через OpenAI-совместимый endpoint (LiteLLM / OpenAI /
vLLM / Ollama). Завязки на chromadb нет.

В отличие от `boba.provider.openai.OpenAIEmbedder`, локальный
`_OpenAICompatEmbedder` **не** передаёт `dimensions=` в API: Ollama и
многие OpenAI-совместимые прокси этот параметр не поддерживают и вернут
ошибку. Размерность вектора декларируется отдельно (`embedding_dim` в
cfg) и должна совпадать с тем, что эмбеддер на самом деле возвращает —
проверим первым же batch'ем (lazy probe).

Контракт `EmbedderFactory.create`:
- пустой `model` → `EmbeddingModelNotConfiguredError` (явный fail-fast).
- `dim <= 0` → `EmbeddingModelNotConfiguredError`.
- иначе → `_OpenAICompatEmbedder` поверх `OpenAI(base_url, api_key)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import ClassVar

from openai import OpenAI

from boba.indexing.embedder import Embedder

__all__ = [
    "EmbedderFactory",
    "EmbeddingModelNotConfiguredError",
]


class EmbeddingModelNotConfiguredError(ValueError):
    """Поднимается, когда `embedding_model` пуст или `embedding_dim<=0`."""


class _OpenAICompatEmbedder(Embedder[str]):
    """`Embedder[str]` поверх OpenAI-compat embeddings API без `dimensions=`.

    Подходит для Ollama / LiteLLM / vLLM где `dimensions=` не поддержано.
    Размерность декларируется при создании (`declared_dim`) и проверяется
    первым же успешным batch'ем — несоответствие = fatal (контракт SQL-
    схемы `vector(N)`).
    """

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        client: OpenAI,
        model: str,
        declared_dim: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._model = model
        self._declared_dim = declared_dim
        self._batch_size = batch_size
        self._verified: bool = False

    def embed_documents(
        self, contents: Iterable[str],
    ) -> Iterable[Sequence[float]]:
        for batch in self._batched(contents):
            response = self._client.embeddings.create(
                model=self._model, input=batch,
            )
            for item in response.data:
                self._verify(item.embedding)
                yield list(item.embedding)

    def embed_query(self, content: str) -> Sequence[float]:
        response = self._client.embeddings.create(
            model=self._model, input=content,
        )
        if not response.data:
            msg = f"empty response.data from {self._model!r}"
            raise RuntimeError(msg)
        vec = list(response.data[0].embedding)
        self._verify(vec)
        return vec

    def dim(self) -> int:
        return self._declared_dim

    def _verify(self, vec: Sequence[float]) -> None:
        if self._verified:
            return
        actual = len(vec)
        if actual != self._declared_dim:
            msg = (
                f"embedding_dim mismatch: declared {self._declared_dim}, "
                f"model {self._model!r} returned {actual}-dim vectors. "
                f"Обновите cfg.embedding_dim и пере-создайте HNSW-индекс "
                f"(см. migrations.ensure_vector_index)."
            )
            raise RuntimeError(msg)
        self._verified = True

    def _batched(self, contents: Iterable[str]) -> Iterator[list[str]]:
        it = iter(contents)
        while True:
            batch = list(islice(it, self._batch_size))
            if not batch:
                return
            yield batch


class EmbedderFactory:
    """Фабрика `Embedder[str]` для postgres-KB tools.

    Один экземпляр на плагин: тащит политику валидации, импл-выбор —
    OpenAI-совместимый клиент с явно заданной dim. Параметры берутся
    per-call в `create(...)`, не хранятся.
    """

    def create(
        self,
        *,
        model: str,
        dim: int,
        base_url: str,
        api_key: str,
    ) -> Embedder[str]:
        if not model:
            msg = (
                "embedding_model is empty; задайте "
                "[tool.postgres].embedding_model или "
                "BOBA_TOOL__POSTGRES__EMBEDDING_MODEL."
            )
            raise EmbeddingModelNotConfiguredError(msg)
        if dim <= 0:
            msg = (
                "embedding_dim must be > 0; задайте "
                "[tool.postgres].embedding_dim равным native-dim модели."
            )
            raise EmbeddingModelNotConfiguredError(msg)
        client = OpenAI(
            base_url=base_url or None,
            api_key=api_key or "unused",
        )
        return _OpenAICompatEmbedder(
            client=client,
            model=model,
            declared_dim=dim,
        )
