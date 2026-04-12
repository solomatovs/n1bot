"""Подключаемые middleware для потока CompletionDelta.

CompletionMiddleware — цепочка трансформеров между LLM и LLMStreamConsumer.
ContentToolCallParser — буферизует content, парсит JSON tool calls (Ollama fallback).
"""
from __future__ import annotations

import json
import logging
from typing import Iterator, List

from boba_domain.core.llm_service import CompletionDelta
from boba_domain.core.streaming import StreamTransformer

log = logging.getLogger(__name__)

DeltaTransformer = StreamTransformer[CompletionDelta, CompletionDelta]


class CompletionMiddleware:
    """Цепочка StreamTransformer[CompletionDelta, CompletionDelta].

    Если список пуст — stream проходит без изменений.
    """

    def __init__(self, transformers: List[DeltaTransformer] | None = None) -> None:
        self._transformers = list(transformers or [])

    def process(self, stream: Iterator[CompletionDelta]) -> Iterator[CompletionDelta]:
        for transformer in self._transformers:
            stream = _apply(transformer, stream)
        yield from stream


def _apply(
    transformer: DeltaTransformer,
    stream: Iterator[CompletionDelta],
) -> Iterator[CompletionDelta]:
    for delta in stream:
        yield from transformer.feed(delta)
    yield from transformer.flush()


class ContentToolCallParser(DeltaTransformer):
    """Буферизует content только если похоже на JSON tool call.

    Ollama/LiteLLM возвращают tool calls как JSON в content:
        {"name": "search_documents", "arguments": {"query": "..."}}

    Эвристика:
    - Первый content-token начинается с '{' → буферизуем (подозрение на tool call)
    - Первый content-token НЕ начинается с '{' → flush сразу, далее passthrough
    - flush() в конце: буфер → парсим JSON → tool_call или content
    """

    def __init__(self) -> None:
        self._buffer: List[CompletionDelta] = []
        self._buffering = True  # начинаем с буферизации до первого content-token
        self._decided = False   # решение принято?

    def feed(self, delta: CompletionDelta) -> Iterator[CompletionDelta]:
        # Native tool call — flush и пробросить
        if delta.tool_call_index is not None:
            yield from self._flush_as_content()
            self._buffering = False
            self._decided = True
            yield delta
            return

        # Content delta
        if delta.content is not None:
            if not self._decided:
                # Первый content token — решаем буферизовать или нет
                self._decided = True
                stripped = delta.content.lstrip()
                if stripped.startswith("{") or stripped.startswith("["):
                    self._buffering = True
                    self._buffer.append(delta)
                else:
                    self._buffering = False
                    yield delta
            elif self._buffering:
                self._buffer.append(delta)
            else:
                yield delta
            return

        # Reasoning и прочее — passthrough
        yield delta

    def flush(self) -> Iterator[CompletionDelta]:
        if not self._buffer:
            return

        joined = "".join(d.content for d in self._buffer if d.content)
        tool_call = _try_parse_tool_call(joined)

        if tool_call is not None:
            log.debug("Parsed tool call from content: %s", tool_call["name"])
            args = tool_call["arguments"]
            yield CompletionDelta(
                tool_call_index=0,
                tool_call_id=f"call_{tool_call['name']}",
                tool_call_name=tool_call["name"],
                tool_call_arguments=json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
            )
        else:
            yield from self._buffer

        self._buffer.clear()

    def _flush_as_content(self) -> Iterator[CompletionDelta]:
        yield from self._buffer
        self._buffer.clear()

    def reset(self) -> None:
        self._buffer.clear()
        self._buffering = True
        self._decided = False


def _try_parse_tool_call(text: str) -> dict | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "name" in data and "arguments" in data:
        return data
    return None
