"""
Embedder[T] - интерфейс для векторизации контента типа T
    в embedding-векторы (Sequence[float]), которые хранятся в VectorStore[T]
    и используются для поиска похожих документов

например, str для текстов, bytes для изображений

Generic над типом content:
    TextEmbedder → Embedder[str]
    ImageEmbedder → Embedder[bytes]
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Generic, TypeVar

__all__ = ["Embedder"]

T = TypeVar("T")


class Embedder(ABC, Generic[T]):
    """Преобразует content в вектор; provider-нейтральная абстракция."""

    @abstractmethod
    def embed_documents(self, contents: Iterable[T]) -> Iterable[Sequence[float]]:
        """Векторизация для индексации (потенциально с document-prefix)."""
        ...

    @abstractmethod
    def embed_query(self, content: T) -> Sequence[float]:
        """Векторизация запроса (для асимметричных моделей — с query-prefix)."""
        ...

    @abstractmethod
    def dim(self) -> int:
        """Размерность embedding-вектора"""
        ...
