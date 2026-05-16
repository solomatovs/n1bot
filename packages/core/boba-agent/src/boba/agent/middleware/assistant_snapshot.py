"""AssistantSnapshotMiddleware: delta-события assistant-стрима → snapshot-события.

Накапливает частичное состояние ответа модели (thinking / answer / refusal /
tool_calls) per-request_id в `AssistantMessageChunk`, и на `GenerationDone`
эмитит соответствующие snapshot-события:

    ThinkingToken*          → ThinkingComplete
    AnswerToken*            → AnswerComplete
    RefusalToken*           → RefusalComplete
    ToolCallStreamStarted + ToolCallArgumentDelta*
                            → ToolCallComplete | InvalidToolCallReceived

Persistence (запись Message'ей в `MessageService`) и журналирование событий
(`HistoryRecorderMiddleware`) — вне зоны ответственности этого middleware.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    InvalidToolCallReceived,
    RefusalComplete,
    RefusalToken,
    ThinkingComplete,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallComplete,
    ToolCallStreamStarted,
)
from boba.llm.models import AssistantMessageChunk, RequestId
from boba.patterns import StreamSource


class AssistantSnapshotMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Аггрегирует delta-события assistant-стрима и эмитит snapshot-события."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
    ) -> None:
        self._inner = inner
        self._chunks: dict[RequestId, AssistantMessageChunk] = defaultdict(
            AssistantMessageChunk.empty,
        )

    def name(self) -> str:
        return "AssistantSnapshot"

    def reset(self) -> None:
        self._chunks.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            match event:
                case GenerationStarted(request_id=rid):
                    self._chunks.pop(rid, None)
                    yield event
                case ThinkingToken(request_id=rid, token=t):
                    self._chunks[rid] = self._chunks[rid].with_thinking(t)
                    yield event
                case AnswerToken(request_id=rid, token=t):
                    self._chunks[rid] = self._chunks[rid].with_text(t)
                    yield event
                case RefusalToken(request_id=rid, token=t):
                    self._chunks[rid] = self._chunks[rid].with_refusal(t)
                    yield event
                case ToolCallStreamStarted(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                ):
                    self._chunks[rid] = self._chunks[rid].with_tool_call_start(
                        index=i,
                        tool_call_id=tid,
                        tool_name=tn,
                    )
                    yield event
                case ToolCallArgumentDelta(
                    request_id=rid,
                    index=i,
                    arguments_chunk=a,
                ):
                    self._chunks[rid] = self._chunks[rid].with_tool_call_args(
                        index=i,
                        args_chunk=a,
                    )
                    yield event
                case GenerationDone(request_id=rid):
                    yield from self._flush(rid)
                    yield event
                case _:
                    yield event

    def _flush(self, rid: RequestId) -> Iterator[AgentEvent]:
        chunk = self._chunks.pop(rid, AssistantMessageChunk.empty())
        if chunk.is_empty():
            return

        if chunk.thinking:
            yield ThinkingComplete(request_id=rid, content=chunk.thinking)
        if chunk.content:
            yield AnswerComplete(request_id=rid, content=chunk.content)
        if chunk.refusal:
            yield RefusalComplete(request_id=rid, content=chunk.refusal)

        message = chunk.finalize()
        for tc in message.tool_calls:
            yield ToolCallComplete(request_id=rid, call=tc)
        for itc in message.invalid_tool_calls:
            yield InvalidToolCallReceived(request_id=rid, invalid=itc)
