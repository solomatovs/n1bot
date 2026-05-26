"""Проверки сборки hybrid retriever."""

import pickle
from pathlib import Path

import pytest
from langchain_core.documents import Document

from langgraph_rag.config import Settings
from langgraph_rag.retriever import HybridRetrieverFactory


class TestHybridRetrieverFactory:
    @staticmethod
    def _make_settings(bm25_path: Path) -> Settings:
        return Settings(
            _env_file=None,
            pg_dsn="postgresql+psycopg://u:p@h/db",
            classifier_model="ollama_chat/m",
            keyword_extractor_model="ollama_chat/m",
            answerer_model="ollama_chat/m",
            bm25_corpus_path=bm25_path,
        )

    def test_build_raises_when_bm25_corpus_missing(self, tmp_path: Path) -> None:
        settings = self._make_settings(tmp_path / "absent.pkl")
        factory = HybridRetrieverFactory(settings)
        with pytest.raises(RuntimeError, match="BM25-корпус не найден"):
            factory._build_bm25_retriever()

    def test_load_bm25_corpus_returns_pickled_documents(self, tmp_path: Path) -> None:
        corpus_path = tmp_path / "corpus.pkl"
        docs = [
            Document(page_content="первый чанк", metadata={"src": "a"}),
            Document(page_content="второй чанк", metadata={"src": "b"}),
        ]
        with corpus_path.open("wb") as f:
            pickle.dump(docs, f)

        settings = self._make_settings(corpus_path)
        factory = HybridRetrieverFactory(settings)
        loaded = factory._load_bm25_corpus()

        assert [doc.page_content for doc in loaded] == ["первый чанк", "второй чанк"]
