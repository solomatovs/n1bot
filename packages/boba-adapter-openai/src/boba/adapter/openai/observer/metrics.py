"""Сводка по сырым chunk-ам в logger."""

from __future__ import annotations

import logging
import time
from typing import Any

from boba.adapter.openai.observer.reasoning import MultiKeyReasoningExtractor
from boba_next.llm.observer import LLMRequestObserver, RequestOutcome
from boba.patterns import Converter
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta

logger = logging.getLogger(__name__)


class MetricsChatCompletionObserver(
    LLMRequestObserver[dict[str, Any], ChatCompletionChunk]
):
    """Пишет одну строку-сводку в logger по завершении запроса."""

    def __init__(
        self,
        reasoning_extractor: Converter[ChoiceDelta, str | None] | None = None,
    ) -> None:
        self._reasoning_extractor = (
            reasoning_extractor
            if reasoning_extractor is not None
            else MultiKeyReasoningExtractor()
        )
        self._reset()

    def _reset(self) -> None:
        self._start: float | None = None
        self._model: str | None = None
        self._chunks = 0
        self._content_chars = 0
        self._reasoning_chars = 0
        self._tool_arg_chars = 0
        self._tool_call_indices: set[int] = set()
        self._finish_reason: str | None = None

    def on_request(self, request: dict[str, Any]) -> None:
        self._reset()
        self._start = time.monotonic()
        self._model = request.get("model")
        logger.info("LLM request: model=%s", self._model)

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        self._chunks += 1
        for choice in chunk.choices:
            delta = choice.delta
            if delta.content:
                self._content_chars += len(delta.content)
            reasoning = self._reasoning_extractor.convert(delta)
            if reasoning:
                self._reasoning_chars += len(reasoning)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    self._tool_call_indices.add(tc.index)
                    if tc.function and tc.function.arguments:
                        self._tool_arg_chars += len(tc.function.arguments)
            if choice.finish_reason:
                self._finish_reason = choice.finish_reason

    def on_request_end(self, outcome: RequestOutcome) -> None:
        elapsed = time.monotonic() - self._start if self._start else 0.0
        logger.info(
            "LLM done: %s, model=%s, chunks=%d, content=%d ch, "
            "reasoning=%d ch, tool_calls=%d (args=%d ch), finish=%s, elapsed=%.2fs",
            outcome.label(),
            self._model,
            self._chunks,
            self._content_chars,
            self._reasoning_chars,
            len(self._tool_call_indices),
            self._tool_arg_chars,
            self._finish_reason,
            elapsed,
        )
