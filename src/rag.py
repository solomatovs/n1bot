from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from config import LLM_TIMEOUT, secret
from retrieval import (
    build_sources,
    group_limit_per_page,
    retrieve_docs,
)
from vectorstore import get_vectorstore


@dataclass
class RagContext:
    """Результат подготовки RAG-контекста."""
    client: OpenAI
    messages: list[ChatCompletionMessageParam]
    sources_block: str


def get_openai(base_url: str) -> OpenAI:
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


def _pid_from_meta(md: dict) -> str:
    return str(md.get("page_id") or "unknown")


def prepare_rag_context(
    embed_collection_name: str,
    query: str,
    model: str,
    top_n: int,
    db_path: str,
    llm_base_url: str,
    embedding_model: Optional[str] = None,
    use_multi_query: bool = True,
    mq_variants: int = 3,
    k_per_variant: int = 6,
    variant_offset: int = 0,
    exclude_page_ids: Optional[List[str]] = None,
    answers_per_variant: int = 3,
) -> Optional[RagContext]:
    """Подготавливает RAG-контекст. Возвращает RagContext или None."""
    openai = get_openai(llm_base_url)

    base_api = llm_base_url.replace("/v1", "").rstrip("/")
    vectorstore = get_vectorstore(
        embed_collection_name, db_path=db_path, llm_base_url=base_api, embedding_model=embedding_model
    )

    cands = retrieve_docs(
        vectorstore,
        query,
        use_multi_query=use_multi_query,
        openai=openai,
        llm_model=model,
        k_single=max(top_n, 12),
        mq_variants=mq_variants,
        k_per_variant=k_per_variant,
        total_top=max(top_n, 12),
    )

    if not cands:
        return None

    cands = group_limit_per_page(cands, per_page=1)

    if exclude_page_ids:
        excl = set(str(x) for x in exclude_page_ids)
        cands = [d for d in cands if _pid_from_meta(d.metadata or {}) not in excl]

    start = max(0, variant_offset * answers_per_variant)
    selected = cands[start : start + answers_per_variant] or cands[:answers_per_variant]

    context = "\n\n".join(d.page_content for d in selected)
    sources_block = build_sources(selected)

    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": (
                "Ты — эксперт по корпоративной базе знаний. "
                "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете."
            ),
        },
        {"role": "user", "content": f"Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."},
    ]

    return RagContext(client=openai, messages=messages, sources_block=sources_block)


def generate_answer_with_context(
    embed_collection_name: str,
    query: str,
    model: str,
    top_n: int,
    db_path: str,
    llm_base_url: str,
    embedding_model: Optional[str] = None,
    use_multi_query: bool = True,
    mq_variants: int = 3,
    k_per_variant: int = 6,
    variant_offset: int = 0,
    exclude_page_ids: Optional[List[str]] = None,
    answers_per_variant: int = 3,
) -> str:
    try:
        ctx = prepare_rag_context(
            embed_collection_name=embed_collection_name,
            query=query,
            model=model,
            top_n=top_n,
            db_path=db_path,
            llm_base_url=llm_base_url,
            embedding_model=embedding_model,
            use_multi_query=use_multi_query,
            mq_variants=mq_variants,
            k_per_variant=k_per_variant,
            variant_offset=variant_offset,
            exclude_page_ids=exclude_page_ids,
            answers_per_variant=answers_per_variant,
        )
    except Exception as e:
        return f"Ошибка подготовки контекста: {e}"

    if ctx is None:
        return "Я не нашёл релевантный контекст по вашей коллекции."

    try:
        resp = ctx.client.chat.completions.create(model=model, messages=ctx.messages, temperature=0)  # type: ignore[arg-type]
        answer = resp.choices[0].message.content or ""
    except Exception as e:
        return f"Не удалось сгенерировать ответ: {e}"

    if ctx.sources_block:
        answer = f"{answer}\n\n---\n**Источники:**\n{ctx.sources_block}"
    return answer
