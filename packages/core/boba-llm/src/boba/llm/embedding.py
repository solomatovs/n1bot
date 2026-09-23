"""Порт эмбеддинга: текст -> вектор.

Ошибки:
LlmError — модель не загрузилась, провайдер недоступен, ответил мусором или
    вектором чужой размерности; выпускают реализации порта.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

__all__ = ["EmbeddingModel"]


class EmbeddingModel(ABC):
    """Порт эмбеддинга; реализация знает префиксы своей модели.

    Батч эмбеддится долго: локальный бэкенд обязан уносить инференс с loop'а,
    удалённый — просто ходит по сети.
    """

    @abstractmethod
    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        """Векторы документов для индексации."""
        ...

    @abstractmethod
    async def embed_query(self, content: str) -> Sequence[float]:
        """Вектор запроса; у асимметричных моделей — со своим префиксом."""
        ...

    @abstractmethod
    def dim(self) -> int:
        """Размерность вектора."""
        ...
