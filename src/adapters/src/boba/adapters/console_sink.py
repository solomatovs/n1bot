"""Sink в stdout/stderr с покрытием всех :class:`AgentEvent`-категорий.

Назначение — читаемый отладочный лог хода агентского цикла:

- **Streaming tokens** (:class:`AnswerToken`, :class:`ThinkingToken`,
  :class:`RefusalToken`) пишутся inline, без переноса строки, с
  flush после каждого токена — даёт «живой» поток.
- **Lifecycle markers** (:class:`IterationStarted`,
  :class:`LLMRequestStarted`, :class:`GenerationStarted` и т.п.) —
  одной строкой с префиксом ``[Marker]`` в stdout.
- **Durable messages** (:class:`UserQueryReceived`, ``*Complete``,
  :class:`ToolResultReady`, :class:`AnswerDiscarded`) — компактные
  заголовки/маркеры завершения.
- **User notifications** (:class:`ToolExecutionFailed`,
  :class:`ToolCallFormatFailed`) — в stderr.
- **Terminal failures** (:class:`GenerationFailed`,
  :class:`MaxIterationsReached`, :class:`PromptFailed`,
  :class:`RepeatedFormatFailure`, :class:`PersistenceFailed`) —
  в stderr с префиксом уровня.

Никакой стилизации (цветов, иконок) — обычный текст. Цветной
рендеринг делает UI-sink (chainlit-bridge).

:class:`ToolCallArgumentDelta` намеренно подавляется (поток мелких
JSON-чанков шумит); полный набор аргументов виден через
:class:`ToolCallComplete`.
"""

from __future__ import annotations

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
    """

    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr

    def name(self) -> str:
        return "ConsoleSink"

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:  # noqa: C901, PLR0912, PLR0915
        match event:
            # ── Streaming tokens (inline, без перевода строки) ──────────
            case AnswerToken(token=t):
                self._inline(t)
            case ThinkingToken(token=t):
                self._inline(t)
            case RefusalToken(token=t):
                self._inline(t)

            # ── Lifecycle markers ───────────────────────────────────────
            case IterationStarted(iteration=i, max_iterations=mx):
                self._line(f"\n=== Iteration {i}/{mx} ===")
            case LLMRequestStarted(model=m, messages_count=mc, has_tools=ht):
                self._line(
                    f"[LLMRequest] model={m} messages={mc} has_tools={ht}"
                )
            case GenerationStarted():
                self._line("[GenerationStarted]")
            case GenerationRetried(attempt=a, reason=r, status_code=sc):
                self._line(f"[GenerationRetried] attempt={a} reason={r} status={sc}")
            case ThinkingStarted():
                self._line("\n[thinking]")
            case AnswerStarted():
                self._line("\n[answer]")
            case ToolCallBegin(index=i, tool_name=tn, tool_call_id=tid):
                self._line(f"\n[ToolCall#{i} begin] name={tn} id={tid}")
            case ToolExecutionStarted(tool_name=tn, tool_call_id=tid):
                self._line(f"[ToolExec start] name={tn} id={tid}")
            case GenerationDone(finish_reason=fr):
                tail = "" if fr.is_terminal else " (continuing)"
                prefix = "\n" if fr.is_terminal else ""
                self._line(f"{prefix}[GenerationDone] finish_reason={fr.value}{tail}")

            # ── Durable messages ────────────────────────────────────────
            case UserQueryReceived(query=q):
                self._line(f"[UserQuery] {self._truncate(q)}")
            case ThinkingComplete():
                self._line("")  # newline после потока thinking-токенов
            case AnswerComplete():
                self._line("")  # newline после потока answer-токенов
            case RefusalComplete():
                self._line("")
            case AnswerDiscarded():
                self._line("[AnswerDiscarded] (re-interpreted as tool_call)")
            case ToolCallComplete(tool_name=tn, arguments=a):
                self._line(f"[ToolCall complete] name={tn} args={self._truncate(a)}")
            case ToolResultReady(tool_name=tn, content=c):
                self._line(f"[ToolResult] name={tn} content={self._truncate(c)}")

            # ── User notifications (stderr) ─────────────────────────────
            case ToolExecutionFailed(
                error_kind=k, tool_name=tn, tool_call_id=tid, message=msg
            ):
                self._err(f"[ToolExec failed] kind={k} name={tn} id={tid}: {msg}")
            case ToolCallFormatFailed(error_kind=k, message=msg):
                self._err(f"[ToolCallFormat failed] kind={k}: {msg}")

            # ── Terminal failures (stderr, явный префикс) ───────────────
            case GenerationFailed(error_kind=k, message=msg):
                self._err(f"[FATAL GenerationFailed] kind={k}: {msg}")
            case MaxIterationsReached(limit=lim, iteration=it, message=msg):
                self._err(f"[FATAL MaxIterations] limit={lim} iter={it}: {msg}")
            case PromptFailed(error_kind=k, provider=p, message=msg):
                self._err(f"[FATAL PromptFailed] kind={k} provider={p}: {msg}")
            case RepeatedFormatFailure(count=c, limit=lim, message=msg):
                self._err(f"[FATAL RepeatedFormatFailure] {c}/{lim}: {msg}")
            case PersistenceFailed(error_kind=k, message=msg):
                self._err(f"[FATAL PersistenceFailed] kind={k}: {msg}")

            # ToolCallArgumentDelta, LLMRequestSent — намеренно подавлены.

    # ── helpers ─────────────────────────────────────────────────────────

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
