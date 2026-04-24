"""AssistantMessagePersistenceMiddleware — агрегация стриминговых
событий ответа в ``*Complete`` события и запись assistant-сообщения
в :class:`MessageService`.

Поведение на стриме событий inner'а:

- :class:`GenerationStarted` → сброс per-``request_id`` буферов
  (корректность после retry в LLM-слое, который может перезапустить
  генерацию с нуля — до первого yield'а);
- :class:`AnswerToken` / :class:`ThinkingToken` → аккумуляция
  во внутренние буферы;
- :class:`ToolCallBegin` → регистрация tool call по ``index``;
- :class:`ToolCallArgumentDelta` → аккумуляция arguments-чанков к
  соответствующему ``index``;
- :class:`GenerationDone` → **flush**: эмитит
  :class:`AnswerComplete` / :class:`ThinkingComplete` (если были
  текстовые токены) и :class:`ToolCallComplete` (по одному на каждый
  tool_call index) **перед** самой ``GenerationDone``; затем коммитит
  ``LLMMessage(role="assistant", content=..., tool_calls=...)`` в
  :class:`MessageService`.

Порядок tool_calls в финальном сообщении — по возрастанию
``index`` (как их нумеровал провайдер); chronological-порядок
поступления чанков внутри каждого tool_call сохраняется.

``RefusalComplete`` (из ``RefusalToken``) пока не эмитим — событие
``RefusalToken`` не мэпится в агент-слое. Когда появится, семантика
идёт по той же схеме (accumulate → flush → durable event).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    ThinkingComplete,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSource
from boba.domain.llm.models import LLMMessage, LLMToolCall, RequestId


class AssistantMessagePersistenceMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Aggregates streaming events, flushes *Complete, persists assistant msg."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._message_service = message_service
        self._thinking: dict[RequestId, list[str]] = defaultdict(list)
        self._answer: dict[RequestId, list[str]] = defaultdict(list)
        # per-rid: index → (tool_call_id, tool_name, list[arg_chunks])
        self._tool_calls: dict[
            RequestId,
            dict[int, tuple[str, str, list[str]]],
        ] = defaultdict(dict)

    def name(self) -> str:
        return "AssistantPersistence"

    def reset(self) -> None:
        self._thinking.clear()
        self._answer.clear()
        self._tool_calls.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            match event:
                case GenerationStarted(request_id=rid):
                    self._reset_buffers(rid)
                    yield event
                case ThinkingToken(request_id=rid, token=t):
                    self._thinking[rid].append(t)
                    yield event
                case AnswerToken(request_id=rid, token=t):
                    self._answer[rid].append(t)
                    yield event
                case AnswerDiscarded(request_id=rid):
                    # Strict-парсер переинтерпретировал ответ как tool_call —
                    # сбрасываем накопленный answer-буфер, чтобы он не
                    # попал в AnswerComplete / assistant-msg.
                    self._answer.pop(rid, None)
                    yield event
                case ToolCallBegin(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                ):
                    self._tool_calls[rid][i] = (tid, tn, [])
                    yield event
                case ToolCallArgumentDelta(
                    request_id=rid,
                    index=i,
                    arguments=a,
                ):
                    if i in self._tool_calls[rid]:
                        self._tool_calls[rid][i][2].append(a)
                    yield event
                case GenerationDone(request_id=rid):
                    yield from self._flush(rid)
                    yield event
                case _:
                    yield event

    def _reset_buffers(self, rid: RequestId) -> None:
        self._thinking.pop(rid, None)
        self._answer.pop(rid, None)
        self._tool_calls.pop(rid, None)

    def _flush(self, rid: RequestId) -> Iterator[AgentEvent]:
        thinking_parts = self._thinking.pop(rid, [])
        answer_parts = self._answer.pop(rid, [])
        tool_calls_raw = self._tool_calls.pop(rid, {})

        if thinking_parts:
            yield ThinkingComplete(
                request_id=rid,
                content="".join(thinking_parts),
            )
        if answer_parts:
            yield AnswerComplete(
                request_id=rid,
                content="".join(answer_parts),
            )

        tool_calls: list[LLMToolCall] = []
        for _index, (tid, tn, arg_parts) in sorted(tool_calls_raw.items()):
            arg_str = "".join(arg_parts)
            yield ToolCallComplete(
                request_id=rid,
                tool_call_id=tid,
                tool_name=tn,
                arguments=arg_str,
            )
            tool_calls.append(LLMToolCall(id=tid, name=tn, arguments=arg_str))

        content = "".join(answer_parts)
        if content or tool_calls:
            self._message_service.add(
                LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tuple(tool_calls),
                ),
            )
