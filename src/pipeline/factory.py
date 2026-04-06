"""Фабрика пайплайнов и контекстов."""
from __future__ import annotations

import httpx
from openai import OpenAI

from pipeline.context import PipelineContext
from pipeline.pipeline import QueryPipeline
from pipeline.stages import (
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
from ui.state import AppConfig, PromptParams, SearchParams
from vectorstore import VectorStoreService


def create_default_pipeline() -> QueryPipeline:
    """Стандартный 10-стадийный RAG-пайплайн."""
    return QueryPipeline([
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


def create_pipeline_context(
    query: str,
    collection_name: str,
    model: str,
    search_params: SearchParams,
    prompt_params: PromptParams,
    cfg: AppConfig,
) -> PipelineContext:
    """Создать PipelineContext со всеми зависимостями."""
    client = create_openai_client(cfg)
    vs = VectorStoreService(cfg)

    return PipelineContext(
        query=query,
        collection_name=collection_name,
        model=model,
        search_params=search_params,
        prompt_params=prompt_params,
        retrieval_config=RetrievalConfig(),
        openai_client=client,
        vectorstore_service=vs,
    )


def create_openai_client(cfg: AppConfig) -> OpenAI:
    """Создаёт OpenAI-клиент из AppConfig."""
    http_client = httpx.Client(
        verify=False,
        timeout=float(cfg.llm_timeout),
        headers={
            "Authorization": f"Bearer {cfg.litellm_api_key}",
            "Content-Type": "application/json",
        },
    )
    return OpenAI(
        base_url=cfg.openai_url,
        api_key=cfg.litellm_api_key,
        http_client=http_client,
    )
