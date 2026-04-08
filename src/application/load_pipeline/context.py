"""Контекст load-пайплайна — входные данные и инфраструктура."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union

from application.load_pipeline.chunking import ChunkingStrategy
from application.load_pipeline.events import (
    LoadingDone,
    PageFailed,
    PageLoaded,
    SpaceEnumerated,
)
from domain.config import AppConfig
from domain.loading import ChunkingParams, SpaceLoadParams, StorageParams
from adapters.vectorstore import VectorStoreService

LoadEvent = Union[SpaceEnumerated, PageLoaded, PageFailed, LoadingDone]


@dataclass
class LoadContext:
    """Входные данные и инфраструктура load-пайплайна.

    Контекст содержит только неизменяемые входные данные,
    инъектированную инфраструктуру и ленивый итератор загрузки.
    Промежуточные счётчики живут локально в стадиях.
    """

    # --- Неизменяемые входные данные ---
    collection_name: str
    cfg: AppConfig
    chunking_params: ChunkingParams
    storage_params: StorageParams

    # --- Инфраструктура (инъекция) ---
    chunker: ChunkingStrategy
    vectorstore_service: VectorStoreService

    # --- Embedding модель для этой загрузки ---
    embedding_model: str = ""

    # --- Входные данные загрузки (заполняются фабрикой) ---
    page_ids: List[str] = field(default_factory=list)
    space_key: str = ""
    space_params: Optional[SpaceLoadParams] = None

    # --- Заполняется LoadPagesStage (ленивый итератор загрузки) ---
    loading_events: Optional[Iterator[LoadEvent]] = None

    # --- Заполняется ChunkStage (ленивый итератор ChunkResult для StoreStage) ---
    chunk_results: Optional[Iterator] = None  # Iterator[ChunkResult] — lazy import avoids cycle
