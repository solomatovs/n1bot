"""Декодер потока OpenAI chunks в поток LLMEvent."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from boba.llm.errors import LLMProtocolError
from boba.llm.events import (
    FinishReason,
    LLMAnswerDelta,
    LLMEvent,
    LLMGenerationDone,
    LLMRefusalDelta,
    LLMThinkingDelta,
    LLMToolCallDelta,
)
from boba.llm.models import LLMContext, RequestId
from boba.patterns import Converter, StreamTransformer
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
)

logger = logging.getLogger(__name__)


class FromOpenAIChunkConverter(
    StreamTransformer[LLMContext, ChatCompletionChunk, LLMEvent]
):
    """
    Обрабатывает openai response в stream=True режиме
    Преобразует поток ChatCompletionChunk доменные события LLMEvent
    """

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._reasoning = MultiKeyReasoningExtractor()
        # Чинит коллизии index у параллельных tool_calls до их сборки.
        self._reindexer = DuplicateToolCallIndexReindexer()
        # index -> (id, name); заполняется на первой дельте каждого tool call.
        self._tool_calls: dict[int, tuple[str, str]] = {}

    def name(self) -> str:
        return "FromOpenAIChunkConverter"

    def reset(self) -> None:
        self._reindexer.reset()
        self._tool_calls.clear()

    def stream(
        self, ctx: LLMContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[LLMEvent]:
        for chunk in stream:
            # Reindexer исправляет index у tool call
            # при парралельном вызове нескольких tool
            for choice in self._reindexer.stream(ctx, chunk.choices):
                # генерируем дельты в логическом порядке:
                    # 1. thinking ->
                    # 2. answer ->
                    # 3. refusal ->
                    # 4. tool_calls ->
                    # 5. finish_reason

                delta = choice.delta

                thinking = self._reasoning.convert(delta)
                if thinking:
                    yield LLMThinkingDelta(request_id=self._request_id, token=thinking)

                if delta.content:
                    yield LLMAnswerDelta(
                        request_id=self._request_id, token=delta.content
                    )

                if delta.refusal:
                    yield LLMRefusalDelta(
                        request_id=self._request_id, token=delta.refusal
                    )

                if delta.tool_calls:
                    yield from self._tool_call_deltas(delta.tool_calls)

                if choice.finish_reason:
                    yield self._finish(choice.finish_reason)

    def _tool_call_deltas(
        self, tool_calls: Iterable[ChoiceDeltaToolCall]
    ) -> Iterable[LLMEvent]:
        for tc in tool_calls:
            first = tc.index not in self._tool_calls
            if first:
                if not (tc.id and tc.function and tc.function.name):
                    raise LLMProtocolError(
                        f"ToolCallDelta: первая дельта index={tc.index} без id/name"
                    )
                self._tool_calls[tc.index] = (tc.id, tc.function.name)

            arguments = (tc.function.arguments if tc.function else None) or ""
            # Первая дельта эмитится всегда (регистрирует слот, даже с пустыми args)
            # последующие — только при непустом фрагменте.
            if first or arguments:
                tool_call_id, tool_name = self._tool_calls[tc.index]
                yield LLMToolCallDelta(
                    request_id=self._request_id,
                    index=tc.index,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )

    def _finish(self, finish_reason: str) -> LLMGenerationDone:
        try:
            reason = FinishReason(finish_reason)
        except ValueError as e:
            raise LLMProtocolError(
                f"unknown finish_reason from provider: {finish_reason!r}"
            ) from e
        return LLMGenerationDone(request_id=self._request_id, finish_reason=reason)


class MultiKeyReasoningExtractor(Converter[ChoiceDelta, str | None]):
    """
    Этот класс позволяет обойти проблему того, что
    разные модели эмитят thinking в разные поля
    извлечение thinking происходит в одном из найденных полей в порядке приоритета:
    - reasoning_content
    - thinking
    - reasoning
    """

    DEFAULT_KEYS: tuple[str, ...] = (
        "reasoning_content",
        "thinking",
        "reasoning",
    )

    def __init__(self, keys: tuple[str, ...] | None = None) -> None:
        self._keys = keys if keys is not None else self.DEFAULT_KEYS

    def convert(self, value: ChoiceDelta) -> str | None:
        extra = value.model_extra or {}
        for k in self._keys:
            v = extra.get(k)
            if v:
                return str(v)
        return None


class DuplicateToolCallIndexReindexer(StreamTransformer[LLMContext, Choice, Choice]):
    """
    Перемапливает index поле tool_calls на свободные места
    Это нужно если llm случайно прислала один и тот же index для нескольких tool_call
    Мы внучную исправляем index и далее все компоненты системы думают,
    что index пришел корректный. Далее при ответе в llm отправляются эти индексы
    как будто бы она вызвала этот набор toolcall'ов с этими индексами
    """

    def __init__(self) -> None:
        self._index_owner: dict[int, str] = {}
        self._remap: dict[str, int] = {}
        self._next_free: int = 0

    def name(self) -> str:
        return "DuplicateToolCallIndexReindexer"

    def reset(self) -> None:
        self._index_owner.clear()
        self._remap.clear()
        self._next_free = 0

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[Choice]:
        for choice in stream:
            delta = choice.delta
            if delta is not None and delta.tool_calls:
                for tc in delta.tool_calls:
                    self._rewrite(tc)

            yield choice

    def _rewrite(self, tc: ChoiceDeltaToolCall) -> None:
        original = tc.index
        tc_id = tc.id
        if not tc_id:
            owner = self._index_owner.get(original)
            if owner is not None and owner in self._remap:
                tc.index = self._remap[owner]
            return
        if tc_id in self._remap:
            tc.index = self._remap[tc_id]
            return
        owner = self._index_owner.get(original)
        if owner is None:
            self._index_owner[original] = tc_id
            if original >= self._next_free:
                self._next_free = original + 1
            return
        if owner != tc_id:
            self._assign_new_index(tc, tc_id, original)

    def _assign_new_index(
        self, tc: ChoiceDeltaToolCall, tc_id: str, original: int
    ) -> None:
        new_index = self._next_free
        self._next_free += 1
        self._index_owner[new_index] = tc_id
        self._remap[tc_id] = new_index
        tc.index = new_index
        logger.info(
            "tool_call index collision: id=%s remapped %d -> %d",
            tc_id,
            original,
            new_index,
        )
