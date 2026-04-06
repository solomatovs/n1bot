"""Фабрика load-пайплайна и контекста."""
from __future__ import annotations

from typing import List, Optional

from chunking import AdvancedChunker
from load_pipeline.context import LoadContext
from load_pipeline.stages import ChunkAndStoreStage, LoadPagesStage
from pipeline import Pipeline
from ui.state import AppConfig, ChunkingParams, SpaceLoadParams, StorageParams
from vectorstore import VectorStoreService


def create_default_load_pipeline() -> Pipeline:
    """Стандартный 2-стадийный пайплайн загрузки."""
    return Pipeline([
        LoadPagesStage(),
        ChunkAndStoreStage(),
    ])


def create_load_context(
    collection_name: str,
    cfg: AppConfig,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    page_ids: Optional[List[str]] = None,
    space_key: str = "",
    space_params: Optional[SpaceLoadParams] = None,
) -> LoadContext:
    """Создать LoadContext со всеми зависимостями."""
    chunker = AdvancedChunker(cfg, chunking_params)
    vs = VectorStoreService(cfg)

    return LoadContext(
        collection_name=collection_name,
        cfg=cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        chunker=chunker,
        vectorstore_service=vs,
        page_ids=page_ids or [],
        space_key=space_key,
        space_params=space_params or SpaceLoadParams(),
    )
