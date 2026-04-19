"""Консольный потребитель AgentEvent."""

from __future__ import annotations

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
    ToolExecutionFailed,
    ToolResultReady,
    UserNoticeReady,
    UserQueryReceived,
)
from boba.domain.agent.meat import AgentContext
from boba.domain.core.patterns import StreamSink


class ConsoleSink(StreamSink[AgentContext, AgentEvent]):
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

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:  # noqa: C901, PLR0912
        match event:
            case ThinkingStarted():
                print(f"{self._DIM}--- thinking ---{self._RESET}")  # noqa: T201
            case ThinkingToken(token=t):
                print(f"{self._DIM}{t}{self._RESET}", end="", flush=True)  # noqa: T201

            case AnswerStarted():
                print(f"\n{self._DIM}--- answer ---{self._RESET}")  # noqa: T201
            case AnswerToken(token=t):
                print(t, end="", flush=True)  # noqa: T201
            case AnswerDiscarded():
                print(  # noqa: T201
                    f"\n{self._DIM}--- (answer reinterpreted as tool call) ---"
                    f"{self._RESET}"
                )

            case RefusalToken(token=t):
                print(f"{self._RED}{t}{self._RESET}", end="", flush=True)  # noqa: T201

            case ToolCallBegin(tool_name=fn):
                print(f"\n{self._YELLOW}[tool] {fn}{self._RESET}")  # noqa: T201
            case ToolCallArgumentDelta(arguments=args):
                print(f"{self._DIM}{args}{self._RESET}", end="", flush=True)  # noqa: T201
            case ToolResultReady(tool_name=fn, content=c):
                preview = c[: self._PREVIEW] + ("..." if len(c) > self._PREVIEW else "")
                print(f"\n{self._GREEN}[result] {fn}: {preview}{self._RESET}")  # noqa: T201
            case ToolExecutionFailed(
                tool_name=fn, error_kind=kind, message=msg
            ):
                preview = msg[: self._PREVIEW] + (
                    "..." if len(msg) > self._PREVIEW else ""
                )
                print(  # noqa: T201
                    f"\n{self._RED}[tool error: {kind}] {fn}: {preview}{self._RESET}"
                )
            case UserNoticeReady(message=msg, severity=sev):
                color = {
                    "info": self._CYAN,
                    "warning": self._YELLOW,
                    "error": self._RED,
                }[sev]
                print(f"\n{color}[{sev}] {msg}{self._RESET}")  # noqa: T201

            case GenerationDone():
                print()  # noqa: T201

            case GenerationFailed(
                error_kind=kind, message=msg, retryable=retryable, status_code=sc
            ):
                status = f" [status={sc}]" if sc is not None else ""
                tag = "retryable" if retryable else "permanent"
                print(  # noqa: T201
                    f"\n{self._RED}[llm error: {kind} ({tag}){status}] "
                    f"{msg}{self._RESET}"
                )

            case PromptFailed(
                error_kind=kind, message=msg, retryable=retryable, provider=prov
            ):
                tag = "retryable" if retryable else "permanent"
                src = f" [provider={prov}]" if prov else ""
                print(  # noqa: T201
                    f"\n{self._RED}[prompt error: {kind} ({tag}){src}] "
                    f"{msg}{self._RESET}"
                )

            case PersistenceFailed(
                error_kind=kind, message=msg, retryable=retryable
            ):
                tag = "retryable" if retryable else "permanent"
                print(  # noqa: T201
                    f"\n{self._RED}[persistence error: {kind} ({tag})] "
                    f"{msg}{self._RESET}"
                )

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
