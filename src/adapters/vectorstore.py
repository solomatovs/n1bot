"""Сервис для работы с векторным хранилищем ChromaDB."""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from domain.config import AppConfig
from domain.errors import EmbeddingConnectionError
from adapters.embeddings import LiteLLMEmbeddings

log = logging.getLogger(__name__)

EMBEDDING_MODEL_KEY = "embedding_model"


class VectorStoreService:
    """Сервис для работы с векторным хранилищем ChromaDB.

    Поддерживает per-collection embedding модели:
    - При создании коллекции записывает имя модели в metadata
    - При чтении — подбирает нужную модель из кэша
    """

    def __init__(
        self,
        db_path: str,
        default_embedding: LiteLLMEmbeddings,
        cfg: AppConfig,
    ) -> None:
        self._db_path = db_path
        self._cfg = cfg
        self._default_embedding = default_embedding
        self._embedding_cache: dict[str, LiteLLMEmbeddings] = {
            cfg.embedding_model: default_embedding,
        }

    def get_vectorstore(self, collection_name: str) -> Chroma:
        """Получить Chroma vectorstore, автоматически подобрав embedding модель из metadata."""
        client = self._get_client()
        collection = client.get_collection(collection_name)
        model_name = (collection.metadata or {}).get(EMBEDDING_MODEL_KEY)
        if model_name:
            log.info("Collection '%s' uses embedding model: %s", collection_name, model_name)
        else:
            log.warning("Collection '%s' has no embedding model in metadata, using default", collection_name)
        embedding = self.resolve_embedding(model_name)
        return Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

    def create_vectorstore(self, collection_name: str, embedding_model: str) -> Chroma:
        """Создать vectorstore для новой коллекции с привязкой embedding модели."""
        client = self._get_client()
        client.get_or_create_collection(
            collection_name,
            metadata={EMBEDDING_MODEL_KEY: embedding_model},
        )
        embedding = self.resolve_embedding(embedding_model)
        return Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

    def get_collection_embedding_model(self, collection_name: str) -> Optional[str]:
        """Получить имя embedding модели для коллекции (None если не задано)."""
        client = self._get_client()
        try:
            collection = client.get_collection(collection_name)
            return (collection.metadata or {}).get(EMBEDDING_MODEL_KEY)
        except Exception:
            return None

    def store_batch(self, vectorstore: Chroma, docs: Sequence[Document]) -> int:
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
            self._default_embedding.embed_query("test")
        except (ConnectionError, OSError, ValueError) as e:
            raise EmbeddingConnectionError(
                f"Failed to connect to embedding service: {e}"
            ) from e

    def remove_collection(self, name: str) -> None:
        """Удалить коллекцию из ChromaDB."""
        self._get_client().delete_collection(name)

    # -- приватные методы ------------------------------------------------------

    def resolve_embedding(self, model_name: Optional[str], timeout: Optional[int] = None) -> LiteLLMEmbeddings:
        """Получить embedding по имени модели (из кэша или создать новый)."""
        if not model_name:
            return self._default_embedding

        effective_timeout = timeout or self._cfg.embedding_timeout

        if model_name not in self._embedding_cache:
            self._embedding_cache[model_name] = LiteLLMEmbeddings(
                model=model_name,
                base_url=self._cfg.litellm_base_url,
                api_key=self._cfg.litellm_api_key,
                timeout=effective_timeout,
                ssl_verify=self._cfg.ssl_verify,
                auth_headers=self._cfg.litellm_auth_headers,
            )

        return self._embedding_cache[model_name]

    def _get_client(self):  # -> chromadb.ClientAPI
        return chromadb.PersistentClient(
            path=self._db_path,
            settings=Settings(anonymized_telemetry=False),
        )

    @staticmethod
    def _normalize_document(d: Document) -> Document:
        md = dict(d.metadata)
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)
