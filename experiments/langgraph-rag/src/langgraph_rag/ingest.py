"""Confluence → splitter → PGVector + BM25-pickle."""

import logging
import pickle
from typing import ClassVar

from langchain_community.document_loaders import ConfluenceLoader
from langchain_core.documents import Document
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter

from langgraph_rag.config import Settings
from langgraph_rag.embeddings import E5Embeddings


class ConfluenceIngestPipeline:
    """Полный цикл ingest: Confluence → чанки → PGVector + BM25-корпус."""

    _logger: ClassVar[logging.Logger] = logging.getLogger("langgraph_rag.ingest")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(self, recreate: bool) -> None:
        """Запускает полный ingest; recreate=True очищает коллекцию и pickle перед записью."""
        self._validate_confluence_credentials()

        raw_docs = self._load_from_confluence()
        self._logger.info("Загружено страниц из Confluence: %d", len(raw_docs))

        chunks = self._split(raw_docs)
        self._logger.info("Получено чанков после splitter: %d", len(chunks))

        self._write_to_pgvector(chunks, recreate=recreate)
        self._logger.info(
            "Чанки записаны в PGVector коллекцию %s", self._settings.pg_collection
        )

        self._write_bm25_corpus(chunks, recreate=recreate)
        self._logger.info(
            "BM25-корпус сохранён: %s", self._settings.bm25_corpus_path
        )

    def _validate_confluence_credentials(self) -> None:
        if not self._settings.confluence_url:
            raise RuntimeError("Не задан LGRAG_CONFLUENCE_URL")
        if not self._settings.confluence_space_keys:
            raise RuntimeError("Список LGRAG_CONFLUENCE_SPACE_KEYS пуст")

    def _load_from_confluence(self) -> list[Document]:
        docs: list[Document] = []
        for space_key in self._settings.confluence_space_keys:
            self._logger.info("Загрузка пространства Confluence: %s", space_key)
            loader = ConfluenceLoader(
                url=self._settings.confluence_url,
                username=self._settings.confluence_username or None,
                api_key=self._settings.confluence_api_token or None,
                space_key=space_key,
                include_attachments=False,
            )
            docs.extend(loader.load())
        return docs

    def _split(self, docs: list[Document]) -> list[Document]:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self._settings.chunk_size,
            chunk_overlap=self._settings.chunk_overlap,
        )
        return splitter.split_documents(docs)

    def _write_to_pgvector(self, chunks: list[Document], recreate: bool) -> None:
        embeddings = E5Embeddings(
            model_name=self._settings.embedding_model,
            device=self._settings.embedding_device,
            cache_dir=self._settings.embedding_cache_dir,
        )
        store = PGVector(
            embeddings=embeddings,
            collection_name=self._settings.pg_collection,
            connection=self._settings.pg_dsn,
            use_jsonb=True,
            pre_delete_collection=recreate,
        )
        store.add_documents(chunks)

    def _write_bm25_corpus(self, chunks: list[Document], recreate: bool) -> None:
        path = self._settings.bm25_corpus_path
        path.parent.mkdir(parents=True, exist_ok=True)

        existing: list[Document] = []
        if not recreate and path.exists():
            with path.open("rb") as f:
                existing = pickle.load(f)

        merged = existing + chunks
        with path.open("wb") as f:
            pickle.dump(merged, f)
