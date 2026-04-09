"""Фабрика load-пайплайна и контекстов."""
from __future__ import annotations

from typing import TYPE_CHECKING, List

from application.load_pipeline.chunking import AdvancedChunker
from domain.errors import ValidationError
from application.load_pipeline.context import LoadContext
from application.load_pipeline.stages import ChunkStage, LoadPagesStage, StoreStage
from domain.pipeline import Pipeline
from domain.loading import ChunkingParams, ConfluenceLoaderParams, SpaceLoadParams, StorageParams

if TYPE_CHECKING:
    from infrastructure.bootstrap import AppServices


def create_default_load_pipeline() -> Pipeline:
    """Стандартный 3-стадийный пайплайн загрузки."""
    return Pipeline([
        LoadPagesStage(),
        ChunkStage(),
        StoreStage(),
    ])


def create_page_load_context(
    page_ids: List[str],
    collection_name: str,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    services: AppServices,
    embedding_model: str,
    confluence_loader_params: ConfluenceLoaderParams | None = None,
) -> LoadContext:
    """Создать LoadContext для загрузки по page IDs.

    Raises:
        ValidationError: если page_ids пустой или collection_name пустой.
    """
    if not page_ids:
        raise ValidationError("page_ids не может быть пустым")
    if not collection_name:
        raise ValidationError("collection_name не может быть пустым")

    chunker = AdvancedChunker(services.embeddings, chunking_params)
    model = embedding_model or services.cfg.embedding_model

    return LoadContext(
        collection_name=collection_name,
        cfg=services.cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        chunker=chunker,
        vectorstore_service=services.vectorstore_service,
        confluence_loader_params=confluence_loader_params or ConfluenceLoaderParams(),
        embedding_model=model,
        page_ids=page_ids,
    )


def create_space_load_context(
    space_key: str,
    collection_name: str,
    space_params: SpaceLoadParams,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    services: AppServices,
    embedding_model: str,
    confluence_loader_params: ConfluenceLoaderParams | None = None,
) -> LoadContext:
    """Создать LoadContext для загрузки пространства.

    Raises:
        ValidationError: если space_key или collection_name пустой.
    """
    if not space_key:
        raise ValidationError("space_key не может быть пустым")
    if not collection_name:
        raise ValidationError("collection_name не может быть пустым")

    chunker = AdvancedChunker(services.embeddings, chunking_params)
    model = embedding_model or services.cfg.embedding_model

    return LoadContext(
        collection_name=collection_name,
        cfg=services.cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        chunker=chunker,
        vectorstore_service=services.vectorstore_service,
        confluence_loader_params=confluence_loader_params or ConfluenceLoaderParams(),
        embedding_model=model,
        space_key=space_key,
        space_params=space_params,
    )
