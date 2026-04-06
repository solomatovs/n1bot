"""RAG-пайплайн — точка входа, делегирует query_pipeline."""
from __future__ import annotations

from typing import Iterator

from query_pipeline import create_default_pipeline, create_query_context
from query_pipeline.events import ChatEvent, RetrievalStarted
from ui.state import AppConfig, PromptParams, SearchParams


def run_chat_pipeline(
    collection_name: str,
    query: str,
    model: str,
    params: SearchParams,
    prompts: PromptParams,
    cfg: AppConfig,
) -> Iterator[ChatEvent]:
    """Полный RAG-пайплайн как генератор событий.

    Обратно-совместимая обёртка над query_pipeline.
    Сигнатура не изменилась — tabs/chat.py вызывает как раньше.
    """
    yield RetrievalStarted(query=query, collection=collection_name)

    pipeline = create_default_pipeline()
    ctx = create_query_context(
        query=query,
        collection_name=collection_name,
        model=model,
        search_params=params,
        prompt_params=prompts,
        cfg=cfg,
    )
    yield from pipeline.run(ctx)
