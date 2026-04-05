from __future__ import annotations

from dataclasses import dataclass

import httpx
from openai import APIError as OpenAIAPIError
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from config import LLM_TIMEOUT, secret
from errors import EmptyContextError, LLMGenerationError
from retrieval import RetrievalService, build_sources
from ui.state import AppConfig, PromptParams, SearchParams
from vectorstore import VectorStoreService


@dataclass
class RagContext:
    """Результат подготовки RAG-контекста."""
    client: OpenAI
    messages: list[ChatCompletionMessageParam]
    sources_block: str


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------

class RagService:
    """Сервис подготовки RAG-контекста.

    Инкапсулирует инфраструктурные зависимости (поиск, LLM-клиент),
    чтобы методы принимали только параметры конкретного запроса.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._client = self._create_openai_client(cfg.litellm_url)
        vs = VectorStoreService(cfg)
        self._retrieval = RetrievalService(vs, self._client)

    def prepare_context(
        self,
        collection_name: str,
        query: str,
        model: str,
        params: SearchParams,
        prompts: PromptParams,
    ) -> RagContext:
        """Подготавливает RAG-контекст.

        Raises:
            EmptyContextError: если не найдено релевантных документов.
        """
        docs = self._retrieval.search(collection_name, query, model, params)
        if not docs:
            raise EmptyContextError("Не найдено релевантных документов по запросу.")

        selected = docs[:params.answers_per_variant]
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
        """Полный пайплайн: поиск контекста + генерация ответа.

        Raises:
            EmptyContextError: если не найдено релевантных документов.
            LLMGenerationError: если модель не смогла сгенерировать ответ.
        """
        ctx = self.prepare_context(
            collection_name=collection_name,
            query=query,
            model=model,
            params=params,
            prompts=prompts,
        )

        try:
            resp = ctx.client.chat.completions.create(
                model=model, messages=ctx.messages, **params.llm_kwargs(),
            )  # type: ignore[arg-type]
            answer = resp.choices[0].message.content or ""
        except OpenAIAPIError as e:
            raise LLMGenerationError(f"Не удалось сгенерировать ответ: {e}") from e

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
