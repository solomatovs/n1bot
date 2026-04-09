"""Контекст query-пайплайна — изменяемое состояние, проходящее через стадии."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from domain.retrieval import DocumentLike, RetrievalConfig
from domain.chat import PromptParams
from domain.search import SearchParams
from domain.vectorstore import VectorStoreService


@dataclass
class QueryContext:
    """Изменяемое состояние, проходящее через стадии query-пайплайна.

    Неизменяемые входные данные задаются при создании.
    Стадии заполняют Optional-поля по мере выполнения.
    """

    # --- Неизменяемые входные данные ---
    query: str
    collection_name: str
    model: str
    search_params: SearchParams
    prompt_params: PromptParams
    retrieval_config: RetrievalConfig

    # --- Инфраструктура (инъекция) ---
    openai_client: OpenAI
    vectorstore_service: VectorStoreService

    # --- Заполняется ClassifyQueryStage ---
    query_type: Optional[str] = None

    # --- Заполняется GenQueryVariantsStage ---
    query_variants: Optional[List[str]] = None

    # --- Заполняется VectorSearchStage ---
    rank_lists: Optional[List[List[DocumentLike]]] = None
    search_errors: List[str] = field(default_factory=list)

    # --- Заполняется RRFMergeStage ---
    merged_docs: Optional[List[DocumentLike]] = None

    # --- Заполняется RerankStage ---
    reranked_docs: Optional[List[DocumentLike]] = None

    # --- Заполняется GroupByPageStage ---
    grouped_docs: Optional[List[DocumentLike]] = None

    # --- Заполняется TopNSelectionStage ---
    selected_docs: Optional[List[DocumentLike]] = None

    # --- Заполняется ContextAssemblyStage ---
    context_text: Optional[str] = None
    sources_block: Optional[str] = None

    # --- Заполняется BuildMessagesStage ---
    messages: Optional[List[ChatCompletionMessageParam]] = None
