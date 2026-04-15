"""Sink, записывающий AgentEvent в HistoryService с агрегацией токенов."""

from __future__ import annotations

from collections import defaultdict

from boba.domain.agent.models import RequestId
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    RefusalComplete,
    RefusalToken,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
)
from boba.domain.core.history import HistoryService
from boba.domain.core.patterns import StreamSink


class HistorySink(StreamSink[AgentEvent]):
    """Записывает события в журнал истории, агрегируя стриминговые токены."""

    def __init__(self, history: HistoryService) -> None:
        self._history = history
        self._thinking_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._answer_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._refusal_buf: dict[RequestId, list[str]] = defaultdict(list)
        self._tool_args: dict[RequestId, dict[int, tuple[str, str, list[str]]]] = defaultdict(dict)

    def name(self) -> str:
        return "History"

    def handle(self, event: AgentEvent) -> None:
        match event:
            case ThinkingToken(request_id, token=t):
                self._thinking_buf[request_id].append(t)
                return
            case AnswerToken(request_id, token=t):
                self._answer_buf[request_id].append(t)
                return
            case RefusalToken(request_id, token=t):
                self._refusal_buf[request_id].append(t)
                return
            case ToolCallArgumentDelta(request_id, index=i, arguments=args):
                if i in self._tool_args[request_id]:
                    self._tool_args[request_id][i][2].append(args)
                return

            case ToolCallBegin(request_id, index=i, tool_call_id=tid, tool_name=fn):
                self._tool_args[request_id][i] = (tid, fn, [])
                self._write(event)
                return
            case GenerationDone(request_id):
                self._flush_thinking(request_id)
                self._flush_answer(request_id)
                self._flush_refusal(request_id)
                self._flush_tool_calls(request_id)
                self._write(event)
                return
            case AnswerStarted(request_id):
                self._flush_thinking(request_id)
            case ThinkingStarted(request_id):
                self._flush_answer(request_id)

            case _:
                pass

        self._write(event)

    def _flush_thinking(self, request_id: RequestId) -> None:
        buf = self._thinking_buf.pop(request_id, [])
        if buf:
            self._write(ThinkingComplete(request_id=request_id, content="".join(buf)))

    def _flush_answer(self, request_id: RequestId) -> None:
        buf = self._answer_buf.pop(request_id, [])
        if buf:
            self._write(AnswerComplete(request_id=request_id, content="".join(buf)))

    def _flush_refusal(self, request_id: RequestId) -> None:
        buf = self._refusal_buf.pop(request_id, [])
        if buf:
            self._write(RefusalComplete(request_id=request_id, content="".join(buf)))

    def _flush_tool_calls(self, request_id: RequestId) -> None:
        tools = self._tool_args.pop(request_id, {})
        for tid, fn, args_parts in tools.values():
            if args_parts:
                self._write(ToolCallComplete(
                    request_id=request_id,
                    tool_call_id=tid,
                    tool_name=fn,
                    arguments="".join(args_parts),
                ))

    def _write(self, event: AgentEvent) -> None:
        self._history.append(event)
