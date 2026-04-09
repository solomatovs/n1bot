"""Реализация VectorStoreService на основе ChromaDB."""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

import chromadb
import chromadb.errors
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from domain.config import AppConfig
from domain.errors import VectorStoreError
from domain.retrieval import DocumentLike
from domain.vectorstore import CollectionData, CollectionInfo
from adapters.litellm_embeddings import LiteLLMEmbeddings

log = logging.getLogger(__name__)

EMBEDDING_MODEL_KEY = "embedding_model"


class ChromaVectorStoreService:
    """Реализация VectorStoreService на базе ChromaDB.

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

    # -- публичный API (реализация Protocol) -----------------------------------

    def list_collections(self) -> List[CollectionInfo]:
        """Получить список всех коллекций с их embedding моделями."""
        try:
            client = self._get_client()
            collections = client.list_collections()
            return [
                CollectionInfo(
                    name=c.name,
                    embedding_model=self._read_embedding_model(c),
                )
                for c in collections
            ]
        except (chromadb.errors.ChromaError, ValueError, OSError) as e:
            raise VectorStoreError(f"Failed to list collections: {e}") from e

    def get_collection_data(self, collection_name: str) -> CollectionData:
        """Получить содержимое коллекции."""
        try:
            client = self._get_client()
            coll = client.get_collection(collection_name)
            data = coll.get(include=["documents", "metadatas"])
            raw_metadatas = data.get("metadatas") or []
            return CollectionData(
                ids=data.get("ids", []),
                documents=data.get("documents") or [],
                metadatas=[dict(m) for m in raw_metadatas],
            )
        except (chromadb.errors.ChromaError, ValueError, OSError) as e:
            raise VectorStoreError(f"Failed to get collection data: {e}") from e

    def get_collection_embedding_model(self, collection_name: str) -> Optional[str]:
        """Получить имя embedding модели для коллекции (None если не задано)."""
        try:
            client = self._get_client()
            collection = client.get_collection(collection_name)
            return self._read_embedding_model(collection)
        except Exception:
            return None

    def create_collection(self, collection_name: str, embedding_model: str) -> None:
        """Создать коллекцию с привязкой embedding модели."""
        try:
            client = self._get_client()
            client.get_or_create_collection(
                collection_name,
                metadata={EMBEDDING_MODEL_KEY: embedding_model},
            )
        except chromadb.errors.ChromaError as e:
            raise VectorStoreError(f"Failed to create collection: {e}") from e

    def remove_collection(self, name: str) -> None:
        """Удалить коллекцию."""
        try:
            self._get_client().delete_collection(name)
        except chromadb.errors.ChromaError as e:
            raise VectorStoreError(f"Failed to remove collection: {e}") from e

    def store_batch(self, collection_name: str, docs: Sequence[DocumentLike]) -> int:
        """Сохранить батч документов в коллекцию. Возвращает количество сохранённых."""
        try:
            vectorstore = self._get_langchain_vectorstore(collection_name)
            normalized = [self._normalize_document(d) for d in docs]
            vectorstore.add_documents(normalized)
            return len(normalized)
        except chromadb.errors.ChromaError as e:
            raise VectorStoreError(f"Failed to store batch: {e}") from e

    def search(self, collection_name: str, query: str, k: int, filters: dict) -> List[DocumentLike]:
        """Выполнить векторный поиск по коллекции."""
        try:
            vectorstore = self._get_langchain_vectorstore(collection_name)
            retr = vectorstore.as_retriever(search_kwargs={"k": k, "filter": filters})
            raw = retr.invoke(query) or []
            return list(raw)
        except (chromadb.errors.ChromaError, RuntimeError) as e:
            raise VectorStoreError(f"Search failed: {e}") from e

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

    def _get_langchain_vectorstore(self, collection_name: str) -> Chroma:
        """Получить langchain Chroma vectorstore с нужной embedding моделью."""
        client = self._get_client()
        collection = client.get_collection(collection_name)
        model_name = self._read_embedding_model(collection)
        embedding = self.resolve_embedding(model_name)
        return Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

    def _get_client(self):  # noqa: ANN202
        return chromadb.PersistentClient(
            path=self._db_path,
            settings=Settings(anonymized_telemetry=False),
        )

    @staticmethod
    def _read_embedding_model(collection: chromadb.Collection) -> Optional[str]:
        """Прочитать имя embedding модели из metadata коллекции."""
        metadata = collection.metadata or {}
        return metadata.get(EMBEDDING_MODEL_KEY)

    @staticmethod
    def _normalize_document(d: DocumentLike) -> Document:
        md = dict(d.metadata)
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)
