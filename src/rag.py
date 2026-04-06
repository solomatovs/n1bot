"""RAG-пайплайн — точки входа, делегируют query_pipeline."""
from __future__ import annotations

import dataclasses
from typing import Iterator

from bootstrap import AppServices
from query_pipeline.events import ChatEvent, RetrievalStarted
from query_pipeline.factory import create_query_context
from ui.state import PromptParams, SearchParams


def run_chat_pipeline(
    collection_name: str,
    query: str,
    model: str,
    params: SearchParams,
    prompts: PromptParams,
    services: AppServices,
) -> Iterator[ChatEvent]:
    """Полный RAG-пайплайн как генератор событий (retrieval + LLM)."""
    yield RetrievalStarted(query=query, collection=collection_name)

    ctx = create_query_context(
        query=query,
        collection_name=collection_name,
        model=model,
        search_params=params,
        prompt_params=prompts,
        services=services,
    )
    yield from services.query_pipeline.run(ctx)


def run_search_pipeline(
    collection_name: str,
    query: str,
    params: SearchParams,
    prompts: PromptParams,
    services: AppServices,
) -> Iterator[ChatEvent]:
    """Retrieval-only пайплайн — чистый vector search без LLM."""
    yield RetrievalStarted(query=query, collection=collection_name)

    search_params = dataclasses.replace(params, use_multi_query=False)
    ctx = create_query_context(
        query=query,
        collection_name=collection_name,
        model="",
        search_params=search_params,
        prompt_params=prompts,
        services=services,
    )
    ctx.query_variants = [query]
    yield from services.search_pipeline.run(ctx)
