from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from config import LLM_TIMEOUT, secret
from retrieval import (
    build_sources,
    group_limit_per_page,
    retrieve_docs,
)
from ui.state import AppConfig, PromptParams, SearchParams
from vectorstore import get_vectorstore


@dataclass
class RagContext:
    """Результат подготовки RAG-контекста."""
    client: OpenAI
    messages: list[ChatCompletionMessageParam]
    sources_block: str


class RagService:
    """Сервис подготовки RAG-контекста.

    Инкапсулирует инфраструктурные зависимости (БД, LLM-клиент),
    чтобы методы принимали только параметры конкретного запроса.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._base_api = cfg.litellm_url.replace("/v1", "").rstrip("/")
        self._client = self._create_openai_client(cfg.litellm_url)

    def prepare_context(
        self,
        collection_name: str,
        query: str,
        model: str,
        params: SearchParams,
        prompts: PromptParams,
    ) -> Optional[RagContext]:
        """Подготавливает RAG-контекст. Возвращает RagContext или None."""
        vectorstore = get_vectorstore(
            collection_name,
            db_path=self._cfg.chroma_db_path,
            llm_base_url=self._base_api,
            embedding_model=None,
        )

        cands = retrieve_docs(
            vectorstore,
            query,
            use_multi_query=params.use_multi_query,
            openai=self._client,
            llm_model=model,
            k_single=params.top_n,
            mq_variants=params.mq_variants,
            k_per_variant=params.k_per_variant,
            total_top=params.top_n,
            content_types=params.content_types,
        )

        if not cands:
            return None

        cands = group_limit_per_page(cands, per_page=params.per_page)
        selected = cands[:params.answers_per_variant]

        context = "\n\n".join(d.page_content for d in selected)
        sources_block = build_sources(selected)

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": prompts.system_prompt},
            {"role": "user", "content": prompts.format_user_message(context=context, query=query)},
        ]

        return RagContext(client=self._client, messages=messages, sources_block=sources_block)

    def generate_answer(
        self,
        collection_name: str,
        query: str,
        model: str,
        params: SearchParams,
        prompts: PromptParams,
    ) -> str:
        """Полный пайплайн: поиск контекста + генерация ответа."""
        try:
            ctx = self.prepare_context(
                collection_name=collection_name,
                query=query,
                model=model,
                params=params,
                prompts=prompts,
            )
        except Exception as e:
            return f"Ошибка подготовки контекста: {e}"

        if ctx is None:
            return "Я не нашёл релевантный контекст по вашей коллекции."

        try:
            resp = ctx.client.chat.completions.create(
                model=model, messages=ctx.messages, temperature=params.temperature,
            )  # type: ignore[arg-type]
            answer = resp.choices[0].message.content or ""
        except Exception as e:
            return f"Не удалось сгенерировать ответ: {e}"

        if ctx.sources_block:
            answer = f"{answer}\n\n---\n**Источники:**\n{ctx.sources_block}"
        return answer

    @staticmethod
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
