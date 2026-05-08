"""AssistantMessagePersistenceMiddleware: токены → AssistantMessageChunk → commit."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from boba.agent.errors import LLMGenerationFailedError
from boba.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    InvalidToolCallReceived,
    ThinkingComplete,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallComplete,
    ToolCallStreamStarted,
)
from boba.agent.messages import MessageWriter
from boba.agent.orchestrator import AgentContext
from boba.llm.errors import LLMError
from boba.llm.models import (
    AssistantMessageChunk,
    RequestId,
    ToolResultMessage,
)
from boba.patterns import StreamSource


class AssistantMessagePersistenceMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Aggregates streaming events в AssistantMessageChunk; flush → commit."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        writer: MessageWriter,
    ) -> None:
        self._inner = inner
        self._writer = writer
        self._chunks: dict[RequestId, AssistantMessageChunk] = defaultdict(
            AssistantMessageChunk.empty,
        )

    def name(self) -> str:
        return "AssistantPersistence"

    def reset(self) -> None:
        self._chunks.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        try:
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
                    case ToolCallStreamStarted(
                        request_id=rid, index=i, tool_call_id=tid, tool_name=tn,
                    ):
                        self._chunks[rid] = self._chunks[rid].with_tool_call_start(
                            index=i, tool_call_id=tid, tool_name=tn,
                        )
                        yield event
                    case ToolCallArgumentDelta(
                        request_id=rid, index=i, arguments_chunk=a,
                    ):
                        self._chunks[rid] = self._chunks[rid].with_tool_call_args(
                            index=i, args_chunk=a,
                        )
                        yield event
                    case GenerationDone(request_id=rid):
                        yield from self._flush(rid)
                        yield event
                    case _:
                        yield event
        except LLMError as e:
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e

    def _flush(self, rid: RequestId) -> Iterator[AgentEvent]:
        chunk = self._chunks.pop(rid, AssistantMessageChunk.empty())
        if chunk.is_empty():
            return

        if chunk.thinking:
            yield ThinkingComplete(request_id=rid, content=chunk.thinking)
        if chunk.content:
            yield AnswerComplete(request_id=rid, content=chunk.content)

        message = chunk.finalize()
        for tc in message.tool_calls:
            yield ToolCallComplete(request_id=rid, call=tc)
        for itc in message.invalid_tool_calls:
            yield InvalidToolCallReceived(request_id=rid, invalid=itc)

        self._writer.add(message)

        for itc in message.invalid_tool_calls:
            self._writer.add(
                ToolResultMessage(
                    tool_call_id=itc.id,
                    content=itc.error,
                    success=False,
                ),
            )
