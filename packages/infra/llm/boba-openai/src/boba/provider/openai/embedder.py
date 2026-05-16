"""OpenAI Embedder: text-embedding-3-* и совместимые."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import ClassVar

from boba.indexing.embedder import Embedder
from boba.llm.errors import LLMProtocolError
from openai import OpenAI

__all__ = ["OpenAIEmbedder"]


class OpenAIEmbedder(Embedder[str]):
    """
    Embedder[str] поверх OpenAI Embeddings API (text-embedding-3-*)

    Caller передаёт уже сконфигурированный OpenAI client (см.
    `boba.provider.openai.terminal.build_openai_client`). Параметр
    `dimensions` передаётся в API при каждом вызове и совпадает с тем,
    что вернёт `dim()` — поэтому подходит только для моделей,
    поддерживающих кастомный output-размер (text-embedding-3-*)

    `embed_documents` батчит вход через `batch_size`. Для асимметричных
    моделей с document/query-префиксами нужен отдельный embedder
    """

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        client: OpenAI,
        model: str,
        dimensions: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._model = model
        self._dim = dimensions
        self._batch_size = batch_size

    def embed_documents(self, contents: Iterable[str]) -> Iterable[Sequence[float]]:
        for batch in self._batched(contents):
            response = self._client.embeddings.create(
                model=self._model,
                input=batch,
                dimensions=self._dim,
            )

            for item in response.data:
                yield item.embedding

    def embed_query(self, content: str) -> Sequence[float]:
        response = self._client.embeddings.create(
            model=self._model,
            input=content,
            dimensions=self._dim,
        )
        if not response.data:
            raise LLMProtocolError(
                f"OpenAIEmbedder.embed_query: пустой response.data от модели {self._model!r}"
            )
        return response.data[0].embedding

    def dim(self) -> int:
        return self._dim

    def _batched(self, contents: Iterable[str]) -> Iterator[list[str]]:
        while True:
            batch = list(islice(iter(contents), self._batch_size))
            if not batch:
                return

            yield batch
