"""
Sink в stdout/stderr с покрытием всех :class:`AgentEvent`-категорий.
Назначение — читаемый отладочный лог хода агентского цикла:
"""

from __future__ import annotations

import os
from typing import TextIO

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
    GenerationRetried,
    GenerationStarted,
    IterationStarted,
    LLMRequestStarted,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    RepeatedFormatFailure,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSink


class ConsoleSink(StreamSink[AgentContext, AgentEvent]):
    """AgentEvent → stdout/stderr.

    Принимает любые :class:`TextIO` — удобно для stdout/stderr и
    для подмены в тестах.

    ``use_color``:
      * ``None`` (по умолчанию) — авто: включаем, если оба потока —
        TTY и не выставлена переменная окружения ``NO_COLOR``.
      * ``True`` / ``False`` — явный override.
    """

    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
        use_color: bool | None = None,
    ) -> None:
        self._RESET = "\x1b[0m"
        self._DIM = "\x1b[2m"
        self._GREEN = "\x1b[32m"
        self._YELLOW = "\x1b[33m"
        self._CYAN = "\x1b[36m"
        self._MAGENTA = "\x1b[35m"
        self._BOLD_RED = "\x1b[1;31m"
        self._BOLD_CYAN = "\x1b[1;36m"
        self._BOLD_GREEN = "\x1b[1;32m"
        self._stdout = stdout
        self._stderr = stderr
        self._color = self._resolve_color(stdout, stderr, use_color)

    @staticmethod
    def _resolve_color(
        stdout: TextIO, stderr: TextIO, use_color: bool | None
    ) -> bool:
        if use_color is not None:
            return use_color
        if os.environ.get("NO_COLOR"):
            return False
        return bool(
            getattr(stdout, "isatty", lambda: False)()
            and getattr(stderr, "isatty", lambda: False)()
        )

    def name(self) -> str:
        return "ConsoleSink"

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:  # noqa: C901, PLR0912, PLR0915
        match event:
            # ── Streaming tokens (inline, без перевода строки) ──────────
            case AnswerToken(token=t):
                self._inline(t)  # answer — без подкраски (основной текст)
            case ThinkingToken(token=t):
                self._inline(self._paint(t, self._DIM))
            case RefusalToken(token=t):
                self._inline(self._paint(t, self._YELLOW))

            # ── Lifecycle markers ───────────────────────────────────────
            case IterationStarted(iteration=i, max_iterations=mx):
                self._line(
                    self._paint(f"\n=== Iteration {i}/{mx} ===", self._BOLD_CYAN)
                )
            case LLMRequestStarted(model=m, messages_count=mc, has_tools=ht):
                self._line(
                    self._paint(
                        f"[LLMRequest] model={m} messages={mc} has_tools={ht}",
                        self._DIM,
                    )
                )
            case GenerationStarted():
                self._line(self._paint("[GenerationStarted]", self._DIM))
            case GenerationRetried(attempt=a, reason=r, status_code=sc):
                self._line(
                    self._paint(
                        f"[GenerationRetried] attempt={a} reason={r} status={sc}",
                        self._YELLOW,
                    )
                )
            case ThinkingStarted():
                self._line(self._paint("\n[thinking]", self._CYAN))
            case AnswerStarted():
                self._line(self._paint("\n[answer]", self._BOLD_CYAN))
            case ToolCallBegin(index=i, tool_name=tn, tool_call_id=tid):
                self._line(
                    self._paint(
                        f"\n[ToolCall#{i} begin] name={tn} id={tid}",
                        self._MAGENTA,
                    )
                )
            case ToolExecutionStarted(tool_name=tn, tool_call_id=tid):
                self._line(
                    self._paint(
                        f"[ToolExec start] name={tn} id={tid}",
                        self._MAGENTA,
                    )
                )
            case GenerationDone(finish_reason=fr):
                tail = "" if fr.is_terminal else " (continuing)"
                prefix = "\n" if fr.is_terminal else ""
                self._line(
                    self._paint(
                        f"{prefix}[GenerationDone] finish_reason={fr.value}{tail}",
                        self._DIM,
                    )
                )

            # ── Durable messages ────────────────────────────────────────
            case UserQueryReceived(query=q):
                self._line(
                    self._paint(f"[UserQuery] {self._truncate(q)}", self._BOLD_GREEN)
                )
            case ThinkingComplete():
                self._line("")  # newline после потока thinking-токенов
            case AnswerComplete():
                self._line("")  # newline после потока answer-токенов
            case RefusalComplete():
                self._line("")
            case AnswerDiscarded():
                self._line(
                    self._paint(
                        "[AnswerDiscarded] (re-interpreted as tool_call)",
                        self._YELLOW,
                    )
                )
            case ToolCallComplete(tool_name=tn, arguments=a):
                self._line(
                    self._paint(
                        f"[ToolCall complete] name={tn} args={self._truncate(a)}",
                        self._MAGENTA,
                    )
                )
            case ToolResultReady(tool_name=tn, content=c):
                self._line(
                    self._paint(
                        f"[ToolResult] name={tn} content={self._truncate(c)}",
                        self._GREEN,
                    )
                )

            # ── User notifications (stderr) ─────────────────────────────
            case ToolExecutionFailed(
                error_kind=k, tool_name=tn, tool_call_id=tid, message=msg
            ):
                self._err(
                    self._paint(
                        f"[ToolExec failed] kind={k} name={tn} id={tid}: {msg}",
                        self._YELLOW,
                    )
                )
            case ToolCallFormatFailed(error_kind=k, message=msg):
                self._err(
                    self._paint(
                        f"[ToolCallFormat failed] kind={k}: {msg}",
                        self._YELLOW,
                    )
                )

            # ── Terminal failures (stderr, явный префикс) ───────────────
            case GenerationFailed(error_kind=k, message=msg):
                self._err(
                    self._paint(
                        f"[FATAL GenerationFailed] kind={k}: {msg}",
                        self._BOLD_RED,
                    )
                )
            case MaxIterationsReached(limit=lim, iteration=it, message=msg):
                self._err(
                    self._paint(
                        f"[FATAL MaxIterations] limit={lim} iter={it}: {msg}",
                        self._BOLD_RED,
                    )
                )
            case PromptFailed(error_kind=k, provider=p, message=msg):
                self._err(
                    self._paint(
                        f"[FATAL PromptFailed] kind={k} provider={p}: {msg}",
                        self._BOLD_RED,
                    )
                )
            case RepeatedFormatFailure(count=c, limit=lim, message=msg):
                self._err(
                    self._paint(
                        f"[FATAL RepeatedFormatFailure] {c}/{lim}: {msg}",
                        self._BOLD_RED,
                    )
                )
            case PersistenceFailed(error_kind=k, message=msg):
                self._err(
                    self._paint(
                        f"[FATAL PersistenceFailed] kind={k}: {msg}",
                        self._BOLD_RED,
                    )
                )

            # ToolCallArgumentDelta, LLMRequestSent — намеренно подавлены.

    # ── helpers ─────────────────────────────────────────────────────────

    def _paint(self, text: str, code: str) -> str:
        if not self._color:
            return text
        return f"{code}{text}{self._RESET}"

    def _inline(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    def _line(self, text: str) -> None:
        self._stdout.write(text + "\n")
        self._stdout.flush()

    def _err(self, text: str) -> None:
        self._stderr.write(text + "\n")
        self._stderr.flush()

    _MAX_PREVIEW = 200

    @classmethod
    def _truncate(cls, text: str) -> str:
        if len(text) <= cls._MAX_PREVIEW:
            return text
        return text[: cls._MAX_PREVIEW] + f"... (+{len(text) - cls._MAX_PREVIEW} chars)"
