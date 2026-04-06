from __future__ import annotations

import logging
from typing import List

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from embeddings import LiteLLMEmbeddings
from errors import EmbeddingConnectionError
from ui.state import AppConfig

log = logging.getLogger(__name__)


class VectorStoreService:
    """Сервис для работы с векторным хранилищем ChromaDB.

    Без зависимости от Streamlit — чистый инфраструктурный слой.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._db_path = cfg.chroma_db_path
        self._base_url = cfg.litellm_base_url
        self._api_key = cfg.litellm_api_key
        self._embedding_model = cfg.embedding_model
        self._embedding_timeout = cfg.embedding_timeout

    def get_vectorstore(self, collection_name: str) -> Chroma:
        """Получить Chroma vectorstore для указанной коллекции."""
        client = self._get_client()
        embedding = self._create_embedding()
        return Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

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
            self._create_embedding().embed_query("test")
        except (ConnectionError, OSError, ValueError) as e:
            raise EmbeddingConnectionError(
                f"Не удалось подключиться к сервису эмбеддингов: {e}"
            ) from e

    def remove_collection(self, name: str) -> None:
        """Удалить коллекцию из ChromaDB."""
        self._get_client().delete_collection(name)

    # -- приватные методы ------------------------------------------------------

    def _get_client(self):  # -> chromadb.PersistentClient
        return chromadb.PersistentClient(
            path=self._db_path,
            settings=Settings(anonymized_telemetry=False),
        )

    def _create_embedding(self) -> LiteLLMEmbeddings:
        return LiteLLMEmbeddings(
            model=self._embedding_model,
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._embedding_timeout,
        )

    @staticmethod
    def _normalize_document(d: Document) -> Document:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)
