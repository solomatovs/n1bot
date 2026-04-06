"""Фабрика load-пайплайна и контекста."""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from chunking import AdvancedChunker
from load_pipeline.context import LoadContext
from load_pipeline.stages import ChunkAndStoreStage, LoadPagesStage
from pipeline import Pipeline
from ui.state import ChunkingParams, SpaceLoadParams, StorageParams

if TYPE_CHECKING:
    from bootstrap import AppServices


def create_default_load_pipeline() -> Pipeline:
    """Стандартный 2-стадийный пайплайн загрузки."""
    return Pipeline([
        LoadPagesStage(),
        ChunkAndStoreStage(),
    ])


def create_load_context(
    collection_name: str,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    services: AppServices,
    embedding_model: str = "",
    page_ids: Optional[List[str]] = None,
    space_key: str = "",
    space_params: Optional[SpaceLoadParams] = None,
) -> LoadContext:
    """Создать LoadContext из AppServices."""
    model = embedding_model or services.cfg.embedding_model
    embedding = services.vectorstore_service._resolve_embedding(model)
    chunker = AdvancedChunker(embedding, chunking_params)

    return LoadContext(
        collection_name=collection_name,
        cfg=services.cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        chunker=chunker,
        vectorstore_service=services.vectorstore_service,
        embedding_model=model,
        page_ids=page_ids or [],
        space_key=space_key,
        space_params=space_params or SpaceLoadParams(),
    )
