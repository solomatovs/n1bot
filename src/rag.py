"""RAG-пайплайн — генераторный стриминг: поиск → промпт → LLM → события."""
from __future__ import annotations

from typing import Iterator

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from config import LLM_TIMEOUT, secret
from errors import EmptyContextError
from events import (
    AnswerToken,
    ChatEvent,
    GenerationDone,
    RetrievalDone,
    RetrievalStarted,
    ThinkingToken,
)
from retrieval import RetrievalService, build_sources
from ui.state import AppConfig, PromptParams, SearchParams
from vectorstore import VectorStoreService


# ---------------------------------------------------------------------------
# Парсинг <think> тегов — stateful, но только bool-флаг
# ---------------------------------------------------------------------------

def _parse_think_tags(token: str, in_think: bool) -> Iterator[tuple[str, str, bool]]:
    """Разбирает токен на фрагменты (role, text, new_in_think).

    Yields:
        ("thinking", text, in_think) или ("answer", text, in_think)
    """
    buf = token
    while buf:
        if in_think:
            end = buf.find("</think>")
            if end != -1:
                yield ("thinking", buf[:end], False)
                buf = buf[end + len("</think>"):]
                in_think = False
            else:
                yield ("thinking", buf, True)
                buf = ""
        else:
            start = buf.find("<think>")
            if start != -1:
                if start > 0:
                    yield ("answer", buf[:start], True)
                buf = buf[start + len("<think>"):]
                in_think = True
            else:
                yield ("answer", buf, False)
                buf = ""


# ---------------------------------------------------------------------------
# Генераторный пайплайн чата
# ---------------------------------------------------------------------------

def run_chat_pipeline(
    collection_name: str,
    query: str,
    model: str,
    params: SearchParams,
    prompts: PromptParams,
    cfg: AppConfig,
) -> Iterator[ChatEvent]:
    """Полный RAG-пайплайн как генератор событий.

    Yield:
        RetrievalStarted  → начало поиска
        RetrievalDone      → найдены документы (context + sources)
        ThinkingToken      → токен размышления
        AnswerToken        → токен ответа
        GenerationDone     → генерация завершена

    Raises:
        EmptyContextError: если не найдено документов.
        RetrievalError: если поиск завершился ошибкой.
    """
    client = _create_openai_client(cfg.litellm_url)
    vs = VectorStoreService(cfg)
    retrieval = RetrievalService(vs, client)

    # Фаза 1: поиск
    yield RetrievalStarted(query=query, collection=collection_name)

    docs = retrieval.search(collection_name, query, model, params)
    if not docs:
        raise EmptyContextError("Не найдено релевантных документов по запросу.")

    selected = docs[:params.answers_per_variant]
    context = "\n\n".join(d.page_content for d in selected)
    sources_block = build_sources(selected)

    yield RetrievalDone(
        documents_found=len(selected),
        context=prompts.format_user_message(context=context, query=query),
        sources_block=sources_block,
    )

    # Фаза 2: стриминг от LLM
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": prompts.system_prompt},
        {"role": "user", "content": prompts.format_user_message(context=context, query=query)},
    ]

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        **params.llm_kwargs(),
    )

    in_think = False

    for chunk in stream:
        delta = chunk.choices[0].delta

        # reasoning_content (DeepSeek / o1 через LiteLLM)
        reasoning = getattr(delta, "reasoning_content", None) or ""
        if reasoning:
            yield ThinkingToken(token=reasoning)

        # content — может содержать <think>...</think>
        content = getattr(delta, "content", None) or ""
        if not content:
            continue

        for role, text, new_in_think in _parse_think_tags(content, in_think):
            in_think = new_in_think
            if not text:
                continue
            if role == "thinking":
                yield ThinkingToken(token=text)
            else:
                yield AnswerToken(token=text)

    yield GenerationDone()


# ---------------------------------------------------------------------------
# Фабрика OpenAI-клиента
# ---------------------------------------------------------------------------

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
