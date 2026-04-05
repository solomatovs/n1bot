from __future__ import annotations

import logging
import time
from typing import List

import chromadb
import chromadb.errors
import streamlit as st
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from config import EMBEDDING_MODEL
from embeddings import LiteLLMEmbeddings
from errors import DocumentStorageError, EmbeddingConnectionError
from ui.state import AppConfig

log = logging.getLogger(__name__)


class VectorStoreService:
    """Сервис для работы с векторным хранилищем ChromaDB.

    Инкапсулирует инфраструктурные зависимости (путь к БД, URL эмбеддингов, API-ключ).
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._db_path = cfg.chroma_db_path
        self._base_url = cfg.litellm_url.replace("/v1", "").rstrip("/")
        self._api_key = cfg.litellm_api_key
        self._embedding_model = EMBEDDING_MODEL

    def get_vectorstore(self, collection_name: str) -> Chroma:
        """Получить Chroma vectorstore для указанной коллекции."""
        client = self._get_client()
        embedding = self._create_embedding()
        return Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

    def store_documents(
        self,
        docs: List[Document],
        collection_name: str,
        batch_size: int = 16,
    ) -> chromadb.Collection:
        """Сохранить документы в ChromaDB с прогрессом.

        Raises:
            EmbeddingConnectionError: если не удалось подключиться к сервису эмбеддингов.
            DocumentStorageError: если ни один документ не удалось сохранить.
        """
        self._verify_embedding_connection()

        client = self._get_client()
        client.get_or_create_collection(name=collection_name)

        embedding = self._create_embedding()
        vectorstore = Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

        docs = [self._normalize_document(d) for d in docs]
        total = len(docs)
        ok, bad = 0, 0
        batch_errors: list[str] = []
        pbar = st.progress(0.0, text="Начинаю загрузку…")

        for i in range(0, total, batch_size):
            batch = docs[i : i + batch_size]
            try:
                time.sleep(0.5)
                vectorstore.add_documents(batch)
                ok += len(batch)
            except chromadb.errors.ChromaError as e:
                msg = f"Батч {i + 1}-{i + len(batch)}: {e}"
                log.warning(msg)
                st.warning(msg)
                batch_errors.append(msg)
                bad += len(batch)
            finally:
                pbar.progress(
                    min((i + batch_size) / max(total, 1), 1.0),
                    text=f"Обработано: {min(i + batch_size, total)} / {total}",
                )

        pbar.empty()

        if ok == 0 and bad > 0:
            raise DocumentStorageError(
                f"Не удалось сохранить ни одного документа из {total}. "
                f"Ошибки: {'; '.join(batch_errors)}"
            )

        st.success(f"Готово. Успешно: {ok}, с ошибкой: {bad}")
        return client.get_collection(collection_name)

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
        )

    def _verify_embedding_connection(self) -> None:
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

    @staticmethod
    def _normalize_document(d: Document) -> Document:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)
