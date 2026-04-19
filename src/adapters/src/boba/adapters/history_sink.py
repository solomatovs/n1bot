"""Sink, записывающий AgentEvent в HistoryService с агрегацией токенов."""

from __future__ import annotations

from collections import defaultdict
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
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
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
from boba.domain.agent.models import AgentContext, RequestId
from boba.domain.core.patterns import StreamSink


class HistorySink(StreamSink[AgentContext, AgentEvent]):
    """Записывает события в журнал истории, агрегируя стриминговые токены."""

    def __init__(self, history: HistoryService) -> None:
        self._history = history
        self._thinking_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._answer_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._refusal_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._tool_args: dict[RequestId, dict[int, tuple[str, str, list[str]]]] = (
            defaultdict(dict)
        )

    def name(self) -> str:
        return "History"

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:  # noqa: C901
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
            case PromptFailed(request_id) | PersistenceFailed(request_id):
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
