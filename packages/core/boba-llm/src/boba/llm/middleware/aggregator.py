"""AssistantAggregator: аккумулирует delta-события LLM-стрима в AssistantMessage.

Сидит как обязательная обёртка над provider-terminal (внутри retry-цепочки),
чтобы убрать ответственность с конкретного провайдера и гарантировать наличие
агрегированного результата (`LLMGenerationResult`) в любом LLM-выводе.

Контракт:
    delta-события (Thinking/Answer/Refusal/ToolCall*Delta) пробрасываются
    наружу как есть, попутно накапливаясь в per-request_id
    `AssistantMessageChunk`. На `LLMGenerationDone` чанк финализируется,
    после чего эмитятся:

      1. per-field *Complete события — только для непустых полей
         (`LLMThinkingComplete`, `LLMAnswerComplete`, `LLMRefusalComplete`,
         `LLMToolCallComplete`, `LLMInvalidToolCallReceived`).
      2. оригинальный `LLMGenerationDone` (пробрасывается как маркер).
      3. `LLMGenerationResult(message, finish_reason)` — всегда, даже при
         пустом chunk'е. Несёт полностью собранный AssistantMessage и
         raw `finish_reason` от провайдера без подмен.

`reset()` чистит накопленные чанки (нужно для retry: на новой попытке
накопленное от провалившейся попытки не должно влиять на результат).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from boba.llm.events import (
    LLMAnswerComplete,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationResult,
    LLMInvalidToolCallReceived,
    LLMRefusalComplete,
    LLMRefusalToken,
    LLMThinkingComplete,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
    LLMToolCallComplete,
)
from boba.llm.models import AssistantMessageChunk, LLMContext, RequestId
from boba.patterns import StreamSource

__all__ = ["AssistantAggregator"]


class AssistantAggregator(StreamSource[LLMContext, LLMEvent]):
    """Накапливает delta-стрим и эмитит `LLMGenerationResult` на завершении."""

    def __init__(self, inner: StreamSource[LLMContext, LLMEvent]) -> None:
        self._inner = inner
        self._chunks: dict[RequestId, AssistantMessageChunk] = defaultdict(
            AssistantMessageChunk.empty,
        )

    def name(self) -> str:
        return f"Aggregator({self._inner.name()})"

    def reset(self) -> None:
        self._chunks.clear()
        self._inner.reset()

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        for event in self._inner.stream(ctx):
            match event:
                case LLMThinkingToken(request_id=rid, token=t):
                    self._chunks[rid].append_thinking(t)
                    yield event
                case LLMAnswerToken(request_id=rid, token=t):
                    self._chunks[rid].append_text(t)
                    yield event
                case LLMRefusalToken(request_id=rid, token=t):
                    self._chunks[rid].append_refusal(t)
                    yield event
                case LLMToolCallBegin(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                ):
                    self._chunks[rid].start_tool_call(
                        index=i,
                        tool_call_id=tid,
                        tool_name=tn,
                    )
                    yield event
                case LLMToolCallArgumentDelta(
                    request_id=rid,
                    index=i,
                    arguments=a,
                ):
                    self._chunks[rid].append_tool_call_args(
                        index=i,
                        args_chunk=a,
                    )
                    yield event
                case LLMGenerationDone(request_id=rid):
                    yield from self._flush(rid, event)
                case _:
                    yield event

    def _flush(
        self,
        rid: RequestId,
        done: LLMGenerationDone,
    ) -> Iterator[LLMEvent]:
        chunk = self._chunks.pop(rid, AssistantMessageChunk.empty())
        message = chunk.finalize()

        if message.thinking:
            yield LLMThinkingComplete(request_id=rid, content=message.thinking)
        if message.content:
            yield LLMAnswerComplete(request_id=rid, content=message.content)
        if message.refusal:
            yield LLMRefusalComplete(request_id=rid, content=message.refusal)
        for tc in message.tool_calls:
            yield LLMToolCallComplete(request_id=rid, call=tc)
        for itc in message.invalid_tool_calls:
            yield LLMInvalidToolCallReceived(request_id=rid, invalid=itc)

        yield done

        yield LLMGenerationResult(
            request_id=rid,
            message=message,
            finish_reason=done.finish_reason,
        )
