"""Embedder: text → vector. Отдельная абстракция, инжектится в VectorStore."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

__all__ = ["Embedder"]


class Embedder(ABC):
    """Преобразует текст в вектор; provider-нейтральная абстракция."""

    @abstractmethod
    def embed_documents(self, texts: Iterable[str]) -> Iterable[Iterable[float]]:
        """Векторизация для индексации (потенциально с document-prefix)."""
        ...

    @abstractmethod
    def embed_query(self, text: str) -> Iterable[float]:
        """Векторизация запроса (для асимметричных моделей — с query-prefix)."""
        ...

    @abstractmethod
    def dim_model(self) -> int:
        """Размерность embedding-вектора"""
        ...
