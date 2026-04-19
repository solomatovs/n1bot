"""Middleware синхронизации состояния диалога с :class:`MessageService`.

Два слоя с одной ответственностью — держать в актуальном состоянии
историю сообщений для LLM:

- :class:`AssistantMessagePersistenceMiddleware` — live-режим: агрегирует
  стриминговые события текущей итерации в assistant-сообщение и коммитит.
- :class:`HistoryReplayMiddleware` — холодный старт: реконструирует диалог
  из :class:`HistoryService` и заливает в :class:`MessageService` перед
  первой итерацией.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalComplete,
    RefusalToken,
    ThinkingComplete,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.history import HistoryService
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext, LLMMessage, LLMToolCall, RequestId
from boba.domain.core.patterns import StreamSource


class AssistantMessagePersistenceMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Агрегирует стриминговые события LLM в assistant-сообщение и
    коммитит его в :class:`MessageService`.

    Наблюдает события от inner-стрима:

    - ``AnswerToken`` → аккумулирует ``content``;
    - ``ThinkingToken`` / ``RefusalToken`` → аккумулируют свои буферы
      (для сопутствующих ``*Complete`` событий);
    - ``ToolCallBegin`` + ``ToolCallArgumentDelta`` → аккумулируют
      tool_calls;
    - ``GenerationStarted`` → сброс буферов (корректность ретраев
      :class:`StupidRetryLLMMiddleware`);
    - ``GenerationDone`` → flush: эмитит ``*Complete`` события в стрим и
      коммитит ``LLMMessage(role="assistant", content=..., tool_calls=...)``
      в :class:`MessageService`.

    Попутно эмитит агрегированные ``*Complete`` события **в стрим** —
    это делает их доступными downstream middleware'ам (например,
    :class:`ToolExecutionMiddleware` ждёт ``ToolCallComplete``) и
    sink'ам. Без этого middleware в стриме живут только токены, а
    ``*Complete`` рождаются отдельно лишь внутри
    :class:`HistorySink` для журнала.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._message_service = message_service
        self._thinking: dict[RequestId, list[str]] = defaultdict(list)
        self._answer: dict[RequestId, list[str]] = defaultdict(list)
        self._refusal: dict[RequestId, list[str]] = defaultdict(list)
        self._tool_calls: dict[RequestId, dict[int, tuple[str, str, list[str]]]] = (
            defaultdict(dict)
        )

    def name(self) -> str:
        return "AssistantPersistence"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:  # noqa: C901
        for event in self._inner.stream(ctx):
            match event:
                case GenerationStarted(request_id=rid):
                    self._reset(rid)
                    yield event
                case ThinkingToken(request_id=rid, token=t):
                    self._thinking[rid].append(t)
                    yield event
                case AnswerToken(request_id=rid, token=t):
                    self._answer[rid].append(t)
                    yield event
                case AnswerDiscarded(request_id=rid):
                    self._answer[rid].clear()
                    yield event
                case RefusalToken(request_id=rid, token=t):
                    self._refusal[rid].append(t)
                    yield event
                case ToolCallBegin(
                    request_id=rid, index=i, tool_call_id=tid, tool_name=fn
                ):
                    self._tool_calls[rid][i] = (tid, fn, [])
                    yield event
                case ToolCallArgumentDelta(request_id=rid, index=i, arguments=a):
                    if i in self._tool_calls[rid]:
                        self._tool_calls[rid][i][2].append(a)
                    yield event
                case GenerationDone(request_id=rid):
                    yield from self._flush(rid)
                    yield event
                case _:
                    yield event

    def _reset(self, rid: RequestId) -> None:
        self._thinking.pop(rid, None)
        self._answer.pop(rid, None)
        self._refusal.pop(rid, None)
        self._tool_calls.pop(rid, None)

    def _flush(self, rid: RequestId) -> Iterator[AgentEvent]:
        thinking_parts = self._thinking.pop(rid, [])
        answer_parts = self._answer.pop(rid, [])
        refusal_parts = self._refusal.pop(rid, [])
        tool_calls_raw = self._tool_calls.pop(rid, {})

        if thinking_parts:
            yield ThinkingComplete(request_id=rid, content="".join(thinking_parts))
        if answer_parts:
            yield AnswerComplete(request_id=rid, content="".join(answer_parts))
        if refusal_parts:
            yield RefusalComplete(request_id=rid, content="".join(refusal_parts))

        tool_calls: list[LLMToolCall] = []
        for _, (tid, fn, args_parts) in sorted(tool_calls_raw.items()):
            args_str = "".join(args_parts)
            yield ToolCallComplete(
                request_id=rid,
                tool_call_id=tid,
                tool_name=fn,
                arguments=args_str,
            )
            tool_calls.append(LLMToolCall(id=tid, name=fn, arguments=args_str))

        content = "".join(answer_parts) or "".join(refusal_parts)
        if content or tool_calls:
            self._message_service.add(
                LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                )
            )


class HistoryReplayMiddleware(StreamSource[AgentContext, AgentEvent]):
    """На первом заходе в стрим реконструирует диалог из
    :class:`HistoryService` и вливает :class:`LLMMessage`-и в
    :class:`MessageService`. На повторных заходах — no-op.

    Читает агрегированные события журнала в хронологическом порядке:

    - ``UserQueryReceived`` → ``LLMMessage(role="user")``;
    - ``AnswerComplete`` + ``ToolCallComplete`` между ними и
      ``GenerationDone`` → ``LLMMessage(role="assistant", content, tool_calls)``
      (один assistant-message на один ``GenerationDone``);
    - ``ToolResultReady`` → ``LLMMessage(role="tool", content, tool_call_id)``;
    - ``ToolExecutionFailed`` → ``LLMMessage(role="tool", message, tool_call_id)``
      (LLM должна увидеть ошибку tool'а при реплее так же, как увидела
      бы её при live-исполнении);
    - ``ToolCallFormatFailed`` → ``LLMMessage(role="user", content=message)``
      (LLM должна увидеть критику формата tool call'а при реплее так же,
      как увидела бы её при live-исполнении).

    Остальные события (токены, ``Stage*``, ``*Started``) игнорируются —
    они уже агрегированы в ``*Complete``.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        history: HistoryService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._history = history
        self._message_service = message_service
        self._replayed = False

    def name(self) -> str:
        return "HistoryReplay"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if not self._replayed:
            self._replayed = True
            self._replay()
        yield from self._inner.stream(ctx)

    def _replay(self) -> None:
        answer: dict[RequestId, str] = {}
        tool_calls: dict[RequestId, list[LLMToolCall]] = defaultdict(list)

        for entry in self._history.entries():
            rid = entry.request_id
            event = entry.event
            match event:
                case UserQueryReceived(query=q):
                    self._message_service.add(LLMMessage(role="user", content=q))
                case AnswerComplete(content=c):
                    answer[rid] = c
                case ToolCallComplete(tool_call_id=tid, tool_name=fn, arguments=a):
                    tool_calls[rid].append(LLMToolCall(id=tid, name=fn, arguments=a))
                case GenerationDone():
                    content = answer.pop(rid, "")
                    calls = tool_calls.pop(rid, [])
                    if content or calls:
                        self._message_service.add(
                            LLMMessage(
                                role="assistant",
                                content=content,
                                tool_calls=calls,
                            )
                        )
                case ToolResultReady(tool_call_id=tid, content=c):
                    self._message_service.add(
                        LLMMessage(role="tool", content=c, tool_call_id=tid)
                    )
                case ToolExecutionFailed(tool_call_id=tid, message=m):
                    self._message_service.add(
                        LLMMessage(role="tool", content=m, tool_call_id=tid)
                    )
                case ToolCallFormatFailed(message=m):
                    self._message_service.add(
                        LLMMessage(role="user", content=m)
                    )
                case _:
                    pass
