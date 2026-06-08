"""OpenAI-compat Embedder: Ollama / LiteLLM / vLLM (без dimensions=)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import ClassVar

from boba.indexing.embedder import Embedder
from boba.llm.errors import LLMProtocolError
from openai import OpenAI

__all__ = ["OpenAICompatEmbedder"]


class OpenAICompatEmbedder(Embedder[str]):
    """
    Embedder[str] поверх OpenAI-compat embeddings API без dimensions=.

    В отличие от OpenAIEmbedder, **не** передаёт dimensions= в API:
    многие OpenAI-совместимые прокси (Ollama, LiteLLM с локальными
    backend'ами, ряд vLLM-сборок) этот параметр не поддерживают и
    возвращают ошибку.

    Размерность вектора **выводится из первого ответа модели** (lazy
    discovery) и кэшируется. После первого embedding'а dim() —
    O(1) lookup. Если caller'у dim нужен до первого embedding'а
    (например, чтобы создать HNSW-индекс ДО upsert'а), dim() сам
    сделает probe-запрос (input="dim-probe") и закэширует результат.
    """

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100
    _PROBE_INPUT: ClassVar[str] = "dim-probe"

    def __init__(
        self,
        client: OpenAI,
        model: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._model = model
        self._batch_size = batch_size
        self._dim: int | None = None

    def embed_documents(
        self, contents: Iterable[str],
    ) -> Iterable[Sequence[float]]:
        for batch in self._batched(contents):
            response = self._client.embeddings.create(
                model=self._model,
                input=batch,
            )
            for item in response.data:
                vec = list(item.embedding)
                self._record_dim(vec)
                yield vec

    def embed_query(self, content: str) -> Sequence[float]:
        response = self._client.embeddings.create(
            model=self._model,
            input=content,
        )
        if not response.data:
            raise LLMProtocolError(
                f"OpenAICompatEmbedder.embed_query: пустой response.data "
                f"от модели {self._model!r}"
            )
        vec = list(response.data[0].embedding)
        self._record_dim(vec)
        return vec

    def dim(self) -> int:
        """Размерность вектора модели — lazy probe при первом вызове."""
        if self._dim is None:
            self._dim = self._probe()
        return self._dim

    def _probe(self) -> int:
        """Probe-запрос, чтобы узнать dim модели."""
        response = self._client.embeddings.create(
            model=self._model,
            input=self._PROBE_INPUT,
        )
        if not response.data:
            raise LLMProtocolError(
                f"OpenAICompatEmbedder._probe: пустой response.data "
                f"от модели {self._model!r}"
            )
        return len(response.data[0].embedding)

    def _record_dim(self, vec: Sequence[float]) -> None:
        """Запомнить dim из первого реального embedding'а (избегаем лишнего probe)."""
        actual = len(vec)
        if self._dim is None:
            self._dim = actual
        elif actual != self._dim:
            raise LLMProtocolError(
                f"OpenAICompatEmbedder: model {self._model!r} returned "
                f"vectors of inconsistent dim — first {self._dim}, "
                f"now {actual}"
            )

    def _batched(self, contents: Iterable[str]) -> Iterator[list[str]]:
        it = iter(contents)
        while True:
            batch = list(islice(it, self._batch_size))
            if not batch:
                return
            yield batch
