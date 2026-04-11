"""Контекст doc-пайплайна — входные данные и промежуточные результаты."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List

from domain.doc_chat import LLMMessage
from domain.vectorstore import VectorStoreService


@dataclass
class DocPipelineContext:
    """Контекст для агентного pipeline чата по документам.

    Содержит данные, необходимые стадиям и инструментам.
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

    # --- История чата ---
    history_path: Path | None = None

    # --- Агентный цикл ---
    max_agent_iterations: int = 10

    # --- LLM messages (обогащается стадиями) ---
    messages: List[LLMMessage] = field(default_factory=list)
