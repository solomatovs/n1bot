"""Контекст doc-пайплайна — входные данные и промежуточные результаты."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List

from domain.doc_search import SearchHit
from domain.vectorstore import VectorStoreService


@dataclass
class DocPipelineContext:
    """Контекст для pipeline чата по документам.

    Содержит только данные, необходимые стадиям.
    Инфраструктурные детали (пути, cfg) остаются в factory.
    """

    # --- Входные данные ---
    query: str
    model: str
    embedding_model: str
    collection_name: str
    source_path: Path
    source_file_path: Callable[[str], Path]
    manifest_path: Path

    # --- Инфраструктура ---
    vectorstore_service: VectorStoreService

    # --- Параметры поиска ---
    top_k: int = 5
    context_expand_lines: int = 20

    # --- Промежуточные результаты (заполняются стадиями) ---
    hits: List[SearchHit] = field(default_factory=list)
    expanded_context: str = ""
    answer: str = ""
