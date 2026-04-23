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
    IterationStarted,
    LLMRequestSent,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    RepeatedFormatFailure,
    StageCompleted,
    StageStarted,
    SystemPromptProcessed,
    SystemPromptProcessingStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserNoticeReady,
    UserPromptProcessed,
    UserPromptProcessingStarted,
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

    def _preview(self, text: str | None) -> str:
        if text is None:
            return "<none>"
        if len(text) <= self._PREVIEW:
            return repr(text)
        return repr(text[: self._PREVIEW] + "...")

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None: 
        match event:
            case ThinkingStarted():
                print(f"{self._DIM}--- thinking ---{self._RESET}")
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
            case ToolExecutionFailed(tool_name=fn, error_kind=kind, message=msg):
                preview = msg[: self._PREVIEW] + (
                    "..." if len(msg) > self._PREVIEW else ""
                )
                print(  # noqa: T201
                    f"\n{self._RED}[tool error: {kind}] {fn}: {preview}{self._RESET}"
                )
            case ToolCallFormatFailed(error_kind=kind, message=msg):
                preview = msg[: self._PREVIEW] + (
                    "..." if len(msg) > self._PREVIEW else ""
                )
                print(  # noqa: T201
                    f"\n{self._RED}[tool call format error: {kind}] "
                    f"{preview}{self._RESET}"
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

            case PersistenceFailed(error_kind=kind, message=msg, retryable=retryable):
                tag = "retryable" if retryable else "permanent"
                print(  # noqa: T201
                    f"\n{self._RED}[persistence error: {kind} ({tag})] "
                    f"{msg}{self._RESET}"
                )

            case MaxIterationsReached(
                error_kind=kind, message=msg, limit=limit, iteration=it
            ):
                print(  # noqa: T201
                    f"\n{self._RED}[max iterations: {kind}] "
                    f"iteration={it} limit={limit}: {msg}{self._RESET}"
                )

            case RepeatedFormatFailure(
                error_kind=kind, message=msg, count=count, limit=limit
            ):
                print(  # noqa: T201
                    f"\n{self._RED}[repeated format failure: {kind}] "
                    f"count={count} limit={limit}: {msg}{self._RESET}"
                )

            case IterationStarted(iteration=i, max_iterations=limit):
                print(  # noqa: T201
                    f"\n{self._DIM}--- iteration {i}/{limit} ---{self._RESET}"
                )

            case SystemPromptProcessingStarted(content_before=before):
                print(  # noqa: T201
                    f"\n{self._DIM}--- system-prompt build start --- "
                    f"before={self._preview(before)}{self._RESET}"
                )
            case SystemPromptProcessed(
                content_before=before, content_after=after, duration_ms=ms
            ):
                print(  # noqa: T201
                    f"{self._DIM}--- system-prompt built in {ms:.1f}ms --- "
                    f"before={self._preview(before)} "
                    f"after={self._preview(after)}{self._RESET}"
                )

            case UserPromptProcessingStarted(content_before=before):
                print(  # noqa: T201
                    f"\n{self._DIM}--- user-prompt build start --- "
                    f"before={self._preview(before)}{self._RESET}"
                )
            case UserPromptProcessed(
                content_before=before, content_after=after, duration_ms=ms
            ):
                print(  # noqa: T201
                    f"{self._DIM}--- user-prompt built in {ms:.1f}ms --- "
                    f"before={self._preview(before)} "
                    f"after={self._preview(after)}{self._RESET}"
                )

            case (
                UserQueryReceived()
                | StageStarted()
                | StageCompleted()
                | LLMRequestSent()
                | GenerationStarted()
                | ThinkingComplete()
                | AnswerComplete()
                | RefusalComplete()
                | ToolCallComplete()
                | ToolExecutionStarted()
            ):
                pass

            case _ as unreachable:
                assert_never(unreachable)
