"""Контекст query-пайплайна — изменяемое состояние, проходящее через стадии."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from langchain_core.documents import Document
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from retrieval import RetrievalConfig
from models import PromptParams, SearchParams
from vectorstore import VectorStoreService


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
    rank_lists: Optional[List[List[Document]]] = None
    search_errors: List[str] = field(default_factory=list)

    # --- Заполняется RRFMergeStage ---
    merged_docs: Optional[List[Document]] = None

    # --- Заполняется RerankStage ---
    reranked_docs: Optional[List[Document]] = None

    # --- Заполняется GroupByPageStage ---
    grouped_docs: Optional[List[Document]] = None

    # --- Заполняется TopNSelectionStage ---
    selected_docs: Optional[List[Document]] = None

    # --- Заполняется ContextAssemblyStage ---
    context_text: Optional[str] = None
    sources_block: Optional[str] = None

    # --- Заполняется BuildMessagesStage ---
    messages: Optional[List[ChatCompletionMessageParam]] = None
