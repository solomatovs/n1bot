"""Middleware синхронизации состояния диалога с :class:`MessageService` и
журналом истории.

Три слоя с одной ответственностью — поддерживать источники правды о
диалоге в согласованном состоянии:

- :class:`AssistantMessagePersistenceMiddleware` — live-режим: агрегирует
  стриминговые события текущей итерации в assistant-сообщение и коммитит
  в :class:`MessageService`.
- :class:`HistoryReplayMiddleware` — холодный старт: реконструирует диалог
  из :class:`HistoryService` и заливает в :class:`MessageService` перед
  первой итерацией.
- :class:`HistoryPersistMiddleware` — пишущий конец пары с
  :class:`HistoryReplayMiddleware`: наблюдает события стрима и дописывает
  их в журнал. Живёт в source-цепочке, а не в sink-пайплайне: журнал —
  несущая часть контракта возобновления диалога, а не опциональный
  наблюдатель. Убрав HistoryReplay+HistoryPersist пару целиком, получаем
  stateless-агента без сессий; убрав только одно из звеньев — скрытую
  поломку.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import assert_never

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
    GenerationStarted,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    RepeatedFormatFailure,
    StageCompleted,
    StageStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolResultReady,
    UserNoticeReady,
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
    :class:`ToolExecutionMiddleware` ждёт ``ToolCallComplete``,
    :class:`HistoryPersistMiddleware` пишет их в журнал) и sink'ам.
    Без этого middleware в стриме живут только токены, и ни
    dowstream-логика, ни журнал не получают сводных событий.
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


class HistoryPersistMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Наблюдает стрим и дописывает события в :class:`HistoryService`.

    Пишущая половина пары с :class:`HistoryReplayMiddleware`: один
    journaling-слой пишет, второй читает на следующем запуске процесса.
    Намеренно живёт в source-цепочке, а не в sink-пайплайне —
    persistence не опциональный observer, её отсутствие ломает
    восстановление диалога.

    Буфера токенов мы держим сами: на терминальных событиях итерации
    (``GenerationDone``, ``GenerationFailed``, ``PromptFailed``,
    ``PersistenceFailed``, ``MaxIterationsReached``,
    ``RepeatedFormatFailure``) дописываем агрегированные ``*Complete``
    события, чтобы журнал содержал связные сообщения даже при сбое
    посреди стрима. Полагаться на ``*Complete``-события из стрима
    нельзя: :class:`AssistantMessagePersistenceMiddleware` эмитит их
    только на ``GenerationDone``, и при обрыве генерации они не
    появятся.

    Размещается **снаружи** от остальных middleware в цепочке (самый
    внешний слой), чтобы через него прошли все события, включая
    эмиссию :class:`AgentErrorRouterMiddleware`
    (``MaxIterationsReached`` и т.п.).
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        history: HistoryService,
    ) -> None:
        self._inner = inner
        self._history = history
        self._thinking_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._answer_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._refusal_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._tool_args: dict[RequestId, dict[int, tuple[str, str, list[str]]]] = (
            defaultdict(dict)
        )

    def name(self) -> str:
        return "HistoryPersist"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        for event in self._inner.stream(ctx):
            self._observe(event)
            yield event

    def _observe(self, event: AgentEvent) -> None:  # noqa: C901, PLR0912
        match event:
            case ThinkingToken(request_id, token=t):
                self._thinking_buf[request_id].append(t)
                self._history.append(event)
            case AnswerToken(request_id, token=t):
                self._answer_buf[request_id].append(t)
                self._history.append(event)
            case RefusalToken(request_id, token=t):
                self._refusal_buf[request_id].append(t)
                self._history.append(event)
            case ToolCallArgumentDelta(request_id, index=i, arguments=args):
                if i in self._tool_args[request_id]:
                    self._tool_args[request_id][i][2].append(args)
                self._history.append(event)
            case ToolCallBegin(request_id, index=i, tool_call_id=tid, tool_name=fn):
                self._tool_args[request_id][i] = (tid, fn, [])
                self._history.append(event)
            case GenerationDone(request_id):
                self._flush_thinking(request_id)
                self._flush_answer(request_id)
                self._flush_refusal(request_id)
                self._flush_tool_calls(request_id)
                self._history.append(event)
            case GenerationFailed(request_id):
                self._flush_thinking(request_id)
                self._flush_answer(request_id)
                self._flush_refusal(request_id)
                self._flush_tool_calls(request_id)
                self._history.append(event)
            case (
                PromptFailed(request_id)
                | PersistenceFailed(request_id)
                | MaxIterationsReached(request_id)
                | RepeatedFormatFailure(request_id)
            ):
                self._flush_thinking(request_id)
                self._flush_answer(request_id)
                self._flush_refusal(request_id)
                self._flush_tool_calls(request_id)
                self._history.append(event)
            case AnswerStarted(request_id):
                self._flush_thinking(request_id)
            case ThinkingStarted(request_id):
                self._flush_answer(request_id)
            case AnswerDiscarded(request_id):
                self._answer_buf.pop(request_id, None)
                self._history.append(event)
            case (
                ToolExecutionFailed()
                | ToolCallFormatFailed()
                | UserNoticeReady()
            ):
                self._history.append(event)
            case (
                UserQueryReceived()
                | StageStarted()
                | StageCompleted()
                | GenerationStarted()
                | ThinkingComplete()
                | AnswerComplete()
                | RefusalComplete()
                | ToolCallComplete()
                | ToolResultReady()
            ):
                pass
            case _ as unreachable:
                assert_never(unreachable)

    def _flush_thinking(self, request_id: RequestId) -> None:
        buf = self._thinking_buf.pop(request_id, [])
        if buf:
            self._history.append(
                ThinkingComplete(request_id=request_id, content="".join(buf))
            )

    def _flush_answer(self, request_id: RequestId) -> None:
        buf = self._answer_buf.pop(request_id, [])
        if buf:
            self._history.append(
                AnswerComplete(request_id=request_id, content="".join(buf))
            )

    def _flush_refusal(self, request_id: RequestId) -> None:
        buf = self._refusal_buf.pop(request_id, [])
        if buf:
            self._history.append(
                RefusalComplete(request_id=request_id, content="".join(buf))
            )

    def _flush_tool_calls(self, request_id: RequestId) -> None:
        tools = self._tool_args.pop(request_id, {})
        for tid, fn, args_parts in tools.values():
            if args_parts:
                self._history.append(
                    ToolCallComplete(
                        request_id=request_id,
                        tool_call_id=tid,
                        tool_name=fn,
                        arguments="".join(args_parts),
                    )
                )
