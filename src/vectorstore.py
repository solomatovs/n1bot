"""Сервис для работы с векторным хранилищем ChromaDB."""
from __future__ import annotations

import logging
from typing import List

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from embeddings import LiteLLMEmbeddings
from errors import EmbeddingConnectionError

log = logging.getLogger(__name__)


class VectorStoreService:
    """Сервис для работы с векторным хранилищем ChromaDB.

    Получает готовый LiteLLMEmbeddings через инъекцию — не создаёт клиентов.
    """

    def __init__(self, db_path: str, embedding: LiteLLMEmbeddings) -> None:
        self._db_path = db_path
        self._embedding = embedding

    def get_vectorstore(self, collection_name: str) -> Chroma:
        """Получить Chroma vectorstore для указанной коллекции."""
        client = self._get_client()
        return Chroma(client=client, collection_name=collection_name, embedding_function=self._embedding)

    def store_batch(self, vectorstore: Chroma, docs: List[Document]) -> int:
        """Сохранить один батч документов. Возвращает количество сохранённых.

        Raises:
            chromadb.errors.ChromaError: при ошибке сохранения.
        """
        normalized = [self._normalize_document(d) for d in docs]
        vectorstore.add_documents(normalized)
        return len(normalized)

    def verify_embedding_connection(self) -> None:
        """Проверить подключение к сервису эмбеддингов.

        Raises:
            EmbeddingConnectionError: если подключение не удалось.
        """
        try:
            self._embedding.embed_query("test")
        except (ConnectionError, OSError, ValueError) as e:
            raise EmbeddingConnectionError(
                f"Не удалось подключиться к сервису эмбеддингов: {e}"
            ) from e

    def remove_collection(self, name: str) -> None:
        """Удалить коллекцию из ChromaDB."""
        self._get_client().delete_collection(name)

    # -- приватные методы ------------------------------------------------------

    def _get_client(self):  # -> chromadb.ClientAPI
        return chromadb.PersistentClient(
            path=self._db_path,
            settings=Settings(anonymized_telemetry=False),
        )

    @staticmethod
    def _normalize_document(d: Document) -> Document:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)
