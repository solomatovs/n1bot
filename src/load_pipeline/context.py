"""Контекст load-пайплайна — входные данные и инфраструктура."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union

from chunking import ChunkingStrategy
from load_pipeline.events import (
    LoadingDone,
    PageFailed,
    PageLoaded,
    SpaceEnumerated,
)
from ui.state import AppConfig, ChunkingParams, SpaceLoadParams, StorageParams
from vectorstore import VectorStoreService

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

    # --- Опциональные входные данные (один из двух) ---
    page_ids: List[str] = field(default_factory=list)
    space_key: str = ""
    space_params: SpaceLoadParams = field(default_factory=SpaceLoadParams)

    # --- Заполняется LoadPagesStage (ленивый итератор загрузки) ---
    loading_events: Optional[Iterator[LoadEvent]] = None
