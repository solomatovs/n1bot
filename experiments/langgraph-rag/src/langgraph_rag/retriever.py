"""Hybrid retriever: PGVector + BM25 через EnsembleRetriever."""

import pickle
from typing import ClassVar

from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_postgres import PGVector

from langgraph_rag.config import Settings
from langgraph_rag.embeddings import E5Embeddings


class HybridRetrieverFactory:
    """Собирает EnsembleRetriever(PGVector + BM25) по текущим Settings."""

    _VECTOR_WEIGHT: ClassVar[float] = 0.5
    _BM25_WEIGHT: ClassVar[float] = 0.5

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(self) -> BaseRetriever:
        """Возвращает hybrid retriever, готовый к invoke."""
        vector_retriever = self._build_vector_retriever()
        bm25_retriever = self._build_bm25_retriever()
        return EnsembleRetriever(
            retrievers=[vector_retriever, bm25_retriever],
            weights=[self._VECTOR_WEIGHT, self._BM25_WEIGHT],
        )

    def _build_vector_retriever(self) -> BaseRetriever:
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
        )
        return store.as_retriever(search_kwargs={"k": self._settings.top_k_vector})

    def _build_bm25_retriever(self) -> BM25Retriever:
        corpus = self._load_bm25_corpus()
        retriever = BM25Retriever.from_documents(corpus)
        retriever.k = self._settings.top_k_bm25
        return retriever

    def _load_bm25_corpus(self) -> list[Document]:
        path = self._settings.bm25_corpus_path
        if not path.exists():
            raise RuntimeError(
                f"BM25-корпус не найден: {path}. "
                f"Запусти `langgraph-rag ingest --recreate` сначала."
            )
        with path.open("rb") as f:
            return pickle.load(f)
