"""Консольный потребитель AgentEvent."""

from __future__ import annotations

from typing import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    StageCompleted,
    StageStarted,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolResultReady,
)
from boba.domain.core.patterns import StreamSink


class ConsoleSink(StreamSink[None, AgentEvent]):
    """Выводит поток AgentEvent в консоль."""

    def name(self) -> str:
        return "Console"

    def consume(self, stream: Iterator[AgentEvent]) -> None:
        for event in stream:
            match event:
                case StageStarted(stage=s):
                    print(f"\n[{s}]", end="", flush=True)
                case StageCompleted(stage=s, detail=d):
                    print(f" → {d}", flush=True)
                case GenerationStarted():
                    print("\n[generation started]")
                case ThinkingStarted():
                    print("\n[thinking] ", end="")
                case ThinkingToken(token=t):
                    print(t, end="", flush=True)
                case AnswerStarted():
                    print("\n[answer] ", end="")
                case AnswerToken(token=t):
                    print(t, end="", flush=True)
                case RefusalToken(token=t):
                    print(f"\n[refusal] {t}", end="", flush=True)
                case ToolCallBegin(index=i, tool_call_id=id, tool_name=name):
                    print(f"\n[tool call #{i}] {name} (id={id})")
                case ToolCallArgumentDelta(index=i, arguments=args):
                    print(args, end="", flush=True)
                case ToolResultReady(tool_name=name, content=c, is_error=err):
                    status = "error" if err else "ok"
                    print(f"\n[tool result] {name} ({status}): {c}")
                case GenerationDone(finish_reason=reason):
                    print(f"\n[generation done] reason={reason}")
                case _:
                    print(f"\n[{type(event).__name__}] {event}")
        print()
