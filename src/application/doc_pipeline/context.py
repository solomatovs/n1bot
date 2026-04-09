"""Контекст doc-пайплайна — входные данные и промежуточные результаты."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List

from domain.doc_search import SearchHit

if TYPE_CHECKING:
    from domain.config import AppConfig
    from domain.vectorstore import VectorStoreService


@dataclass
class DocPipelineContext:
    """Контекст для pipeline чата по документам."""

    # --- Входные данные (заполняются при создании) ---
    folder_path: Path
    query: str
    model: str
    embedding_model: str
    cfg: AppConfig

    # --- Инфраструктура ---
    vectorstore_service: VectorStoreService

    # --- Параметры поиска ---
    collection_name: str = ""
    top_k: int = 5
    context_expand_lines: int = 20

    # --- Промежуточные результаты (заполняются стадиями) ---
    hits: List[SearchHit] = field(default_factory=list)
    expanded_context: str = ""
    answer: str = ""

    def __post_init__(self) -> None:
        if not self.collection_name:
            self.collection_name = self.cfg.collection_name(self.folder_path.name)

    @property
    def boba_path(self) -> Path:
        return self.cfg.boba_path(self.folder_path)

    @property
    def context_path(self) -> Path:
        return self.cfg.context_path(self.folder_path)

    @property
    def chroma_path(self) -> str:
        return str(self.cfg.chroma_path(self.folder_path))

    def context_file_path(self, filename: str) -> Path:
        return self.cfg.context_file_path(self.folder_path, filename)
