"""RAG-пайплайн — точка входа, делегирует pipeline.QueryPipeline."""
from __future__ import annotations

from typing import Iterator

from events import ChatEvent
from pipeline.factory import create_default_pipeline, create_pipeline_context
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

    Обратно-совместимая обёртка над QueryPipeline.
    Сигнатура не изменилась — tabs/chat.py вызывает как раньше.
    """
    pipeline = create_default_pipeline()
    ctx = create_pipeline_context(
        query=query,
        collection_name=collection_name,
        model=model,
        search_params=params,
        prompt_params=prompts,
        cfg=cfg,
    )
    yield from pipeline.run(ctx)
