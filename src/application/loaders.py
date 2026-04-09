"""Application use-case'ы загрузки — оркестрация load_pipeline."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, List

from application.load_pipeline.events import LoadPipelineEvent
from domain.loading import ChunkingParams, ConfluenceLoaderParams, SpaceLoadParams, StorageParams

if TYPE_CHECKING:
    from infrastructure.bootstrap import AppServices


def run_page_pipeline(
    page_ids: List[str],
    collection_name: str,
    services: AppServices,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    embedding_model: str,
    confluence_loader_params: ConfluenceLoaderParams | None = None,
) -> Iterator[LoadPipelineEvent]:
    """Полный пайплайн: загрузка по page IDs -> чанкинг -> сохранение."""
    from application.load_pipeline.factory import create_page_load_context

    ctx = create_page_load_context(
        page_ids=page_ids,
        collection_name=collection_name,
        chunking_params=chunking_params,
        storage_params=storage_params,
        services=services,
        embedding_model=embedding_model,
        confluence_loader_params=confluence_loader_params,
    )
    yield from services.load_pipeline.run(ctx)


def run_space_pipeline(
    space_key: str,
    collection_name: str,
    services: AppServices,
    space_params: SpaceLoadParams,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
    embedding_model: str,
    confluence_loader_params: ConfluenceLoaderParams | None = None,
) -> Iterator[LoadPipelineEvent]:
    """Полный пайплайн: загрузка пространства -> чанкинг -> сохранение."""
    from application.load_pipeline.factory import create_space_load_context

    ctx = create_space_load_context(
        space_key=space_key,
        collection_name=collection_name,
        space_params=space_params,
        chunking_params=chunking_params,
        storage_params=storage_params,
        services=services,
        embedding_model=embedding_model,
        confluence_loader_params=confluence_loader_params,
    )
    yield from services.load_pipeline.run(ctx)
