"""Контракт сервиса эмбеддингов — Protocol для domain-логики."""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class Embeddings(Protocol):
    """Минимальный интерфейс эмбеддингов для domain-кода.

    LiteLLMEmbeddings реализует этот Protocol автоматически.
    """

    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...
