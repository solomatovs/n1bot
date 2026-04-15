"""Консольный потребитель AgentEvent."""

from __future__ import annotations

from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.core.patterns import StreamSink

# ANSI
_DIM = "\033[2m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"


class ConsoleSink(StreamSink[AgentEvent]):
    """Выводит поток AgentEvent в консоль."""

    def name(self) -> str:
        return "Console"

    def handle(self, event: AgentEvent) -> None:
        match event:
            case UserQueryReceived(query=q):
                print(f"\n{_BOLD}{_CYAN}> {q}{_RESET}\n")

            case ThinkingStarted():
                print(f"{_DIM}--- thinking ---{_RESET}")
            case ThinkingToken(token=t):
                print(f"{_DIM}{t}{_RESET}", end="", flush=True)

            case AnswerStarted():
                print(f"\n{_DIM}--- answer ---{_RESET}")
            case AnswerToken(token=t):
                print(t, end="", flush=True)

            case RefusalToken(token=t):
                print(f"{_RED}{t}{_RESET}", end="", flush=True)

            case ToolCallBegin(tool_name=fn):
                print(f"\n{_YELLOW}[tool] {fn}{_RESET}")
            case ToolCallArgumentDelta(arguments=args):
                print(f"{_DIM}{args}{_RESET}", end="", flush=True)
            case ToolResultReady(tool_name=fn, content=c, is_error=err):
                color = _RED if err else _GREEN
                label = "error" if err else "result"
                preview = c[:200] + ("..." if len(c) > 200 else "")
                print(f"\n{color}[{label}] {fn}: {preview}{_RESET}")

            case GenerationDone():
                print()

            case _:
                pass
