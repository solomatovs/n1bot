"""Консольный потребитель AgentEvent."""

from __future__ import annotations

from collections.abc import Iterable
from typing import assert_never

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
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
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.meat import AgentContext
from boba.domain.core.patterns import Stream


class ConsoleSink(Stream[AgentContext, AgentEvent, None]):
    """Выводит поток AgentEvent в консоль."""

    def __init__(self) -> None:
        self._DIM = "\033[2m"
        self._BOLD = "\033[1m"
        self._CYAN = "\033[36m"
        self._YELLOW = "\033[33m"
        self._RED = "\033[31m"
        self._GREEN = "\033[32m"
        self._RESET = "\033[0m"
        self._PREVIEW = 200

    def name(self) -> str:
        return "Console"

    def stream(self, ctx: AgentContext, stream: AgentEvent) -> Iterable[None]:
        yield self.handle(stream)

    def handle(self, event: AgentEvent):  # noqa: C901
        match event:
            case ThinkingStarted():
                print(f"{self._DIM}--- thinking ---{self._RESET}")  # noqa: T201
            case ThinkingToken(token=t):
                print(f"{self._DIM}{t}{self._RESET}", end="", flush=True)  # noqa: T201

            case AnswerStarted():
                print(f"\n{self._DIM}--- answer ---{self._RESET}")  # noqa: T201
            case AnswerToken(token=t):
                print(t, end="", flush=True)  # noqa: T201

            case RefusalToken(token=t):
                print(f"{self._RED}{t}{self._RESET}", end="", flush=True)  # noqa: T201

            case ToolCallBegin(tool_name=fn):
                print(f"\n{self._YELLOW}[tool] {fn}{self._RESET}")  # noqa: T201
            case ToolCallArgumentDelta(arguments=args):
                print(f"{self._DIM}{args}{self._RESET}", end="", flush=True)  # noqa: T201
            case ToolResultReady(tool_name=fn, content=c, is_error=err):
                color = self._RED if err else self._GREEN
                label = "error" if err else "result"
                preview = c[: self._PREVIEW] + ("..." if len(c) > self._PREVIEW else "")
                print(f"\n{color}[{label}] {fn}: {preview}{self._RESET}")  # noqa: T201

            case GenerationDone():
                print()  # noqa: T201

            case (
                UserQueryReceived()
                | StageStarted()
                | StageCompleted()
                | GenerationStarted()
                | ThinkingComplete()
                | AnswerComplete()
                | RefusalComplete()
                | ToolCallComplete()
            ):
                pass

            case _ as unreachable:
                assert_never(unreachable)
