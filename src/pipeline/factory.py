"""Фабрика пайплайнов и контекстов."""
from __future__ import annotations

import httpx
from openai import OpenAI

from config import LLM_TIMEOUT, secret
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
    client = _create_openai_client(cfg.litellm_url)
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


def _create_openai_client(base_url: str) -> OpenAI:
    """Создаёт OpenAI-клиент с отключённой проверкой SSL для liteLLM."""
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    http_client = httpx.Client(
        verify=False,
        timeout=float(LLM_TIMEOUT),
        headers={
            "Authorization": f"Bearer {secret('LITELLM_API_KEY')}",
            "Content-Type": "application/json",
        },
    )
    return OpenAI(
        base_url=base_url,
        api_key=secret("LITELLM_API_KEY"),
        http_client=http_client,
    )
