"""Фабрика query-пайплайна и контекста."""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import Pipeline
from query_pipeline.context import QueryContext
from query_pipeline.stages import (
    BuildMessagesStage,
    ClassifyQueryStage,
    ContextAssemblyStage,
    GenQueryVariantsStage,
    GroupByPageStage,
    LLMStreamStage,
    RerankStage,
    RRFMergeStage,
    TopNSelectionStage,
    VectorSearchStage,
)
from retrieval import RetrievalConfig
from models import PromptParams, SearchParams

if TYPE_CHECKING:
    from bootstrap import AppServices


def create_default_query_pipeline() -> Pipeline:
    """Стандартный 10-стадийный RAG-пайплайн."""
    return Pipeline([
        ClassifyQueryStage(),
        GenQueryVariantsStage(),
        VectorSearchStage(),
        RRFMergeStage(),
        RerankStage(),
        GroupByPageStage(),
        TopNSelectionStage(),
        ContextAssemblyStage(),
        BuildMessagesStage(),
        LLMStreamStage(),
    ])


def create_search_pipeline() -> Pipeline:
    """Retrieval-only пайплайн — чистый vector search без LLM."""
    return Pipeline([
        ClassifyQueryStage(),
        VectorSearchStage(),
        RRFMergeStage(),
        RerankStage(),
        GroupByPageStage(),
        TopNSelectionStage(),
        ContextAssemblyStage(),
    ])


def create_query_context(
    query: str,
    collection_name: str,
    model: str,
    search_params: SearchParams,
    prompt_params: PromptParams,
    services: AppServices,
) -> QueryContext:
    """Создать QueryContext из AppServices."""
    return QueryContext(
        query=query,
        collection_name=collection_name,
        model=model,
        search_params=search_params,
        prompt_params=prompt_params,
        retrieval_config=RetrievalConfig(),
        openai_client=services.openai_client,
        vectorstore_service=services.vectorstore_service,
    )
