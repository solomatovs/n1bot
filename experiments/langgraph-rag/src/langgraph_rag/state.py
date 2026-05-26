"""Состояние LangGraph-графа RAG-workflow."""

from typing import TypedDict

from langchain_core.documents import Document


class RagState(TypedDict, total=False):
    """Полное состояние пайплайна между узлами графа."""

    query: str
    category: str
    keywords: list[str]
    reformulations: list[str]
    hits: list[Document]
    answer: str
