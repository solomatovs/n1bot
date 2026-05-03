"""Sink в stdout/stderr поверх семей событий."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TextIO

from boba.agent.events import (
    Advisory,
    AgentEvent,
    ContentDelta,
    ContentSnapshot,
    PhaseTransition,
    Severity,
    SlotKind,
    Terminal,
)
from boba.agent.models import AgentContext
from boba.patterns import StreamSink


class ConsoleSink(StreamSink[AgentContext, AgentEvent]):
    """AgentEvent → stdout/stderr; use_color=None — авто по TTY/NO_COLOR."""

    # ── Цвета ────────────────────────────────────────────────────────
    _RESET = "\x1b[0m"
    _DIM = "\x1b[2m"
    _GREEN = "\x1b[32m"
    _YELLOW = "\x1b[33m"
    _CYAN = "\x1b[36m"
    _MAGENTA = "\x1b[35m"
    _BLUE = "\x1b[34m"
    _BOLD_RED = "\x1b[1;31m"
    _BOLD_CYAN = "\x1b[1;36m"
    _BOLD_GREEN = "\x1b[1;32m"

    _STREAMING_SLOTS = frozenset({SlotKind.ANSWER, SlotKind.THINKING, SlotKind.REFUSAL})

    _MAX_PREVIEW = 200

    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
        use_color: bool | None = None,
        verbose: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._color = self._resolve_color(stdout, stderr, use_color)
        self._verbose = verbose

    @staticmethod
    def _resolve_color(stdout: TextIO, stderr: TextIO, use_color: bool | None) -> bool:
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

    # ── Главный диспетчер по семьям ──────────────────────────────────

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:
        del ctx
        match event:
            case ContentDelta():
                self._on_delta(event)
            case ContentSnapshot():
                self._on_snapshot(event)
            case PhaseTransition():
                self._on_phase(event)
            case Advisory():
                self._on_advisory(event)
            case Terminal():
                self._on_terminal(event)

    # ── Per-family ───────────────────────────────────────────────────

    def _on_delta(self, e: ContentDelta) -> None:
        chunk = e.chunk()
        if not chunk:
            return
        color = self._color_for_slot(e.slot())
        self._inline(self._paint(chunk, color))

    def _on_snapshot(self, e: ContentSnapshot) -> None:
        slot = e.slot()
        if slot in self._STREAMING_SLOTS:
            self._line("")
            return
        color = self._color_for_slot(slot)
        head_label = slot.value
        if e.headline():
            head_label = f"{head_label}:{e.headline()}"
        head = f"[{head_label}]"
        body = self._truncate(e.body())
        self._line(self._paint(f"{head} {body}", color))

    def _on_phase(self, e: PhaseTransition) -> None:
        color = self._color_for_severity(e.severity())
        details_str = self._fmt_details(e.details())
        self._line(self._paint(f"[{e.label()}]{details_str}", color))
        body = e.body()
        if self._verbose and body:
            self._line(self._paint(self._truncate(body), self._DIM))

    def _on_advisory(self, e: Advisory) -> None:
        color = self._color_for_severity(e.severity())
        details_str = self._fmt_details(e.details())
        self._err(self._paint(f"[WARN] {e.headline()}{details_str}", color))
        body = e.body()
        if body:
            self._err(self._paint(self._truncate(body), self._DIM))

    def _on_terminal(self, e: Terminal) -> None:
        color = self._color_for_severity(e.severity())
        details_str = self._fmt_details(e.details())
        self._err(self._paint(f"[FATAL] {e.headline()}{details_str}", color))
        body = e.body()
        if body:
            self._err(self._paint(self._truncate(body), self._DIM))

    # ── helpers ──────────────────────────────────────────────────────

    def _color_for_slot(self, slot: SlotKind) -> str:
        return {
            SlotKind.ANSWER: "",
            SlotKind.THINKING: self._DIM,
            SlotKind.REFUSAL: self._YELLOW,
            SlotKind.TOOL_ARGS: self._MAGENTA,
            SlotKind.TOOL_CALL: self._MAGENTA,
            SlotKind.TOOL_RESULT: self._GREEN,
            SlotKind.USER_QUERY: self._BOLD_GREEN,
            SlotKind.FEEDBACK: self._BLUE,
        }[slot]

    def _color_for_severity(self, severity: Severity) -> str:
        return {
            Severity.INFO: self._DIM,
            Severity.WARN: self._YELLOW,
            Severity.ERROR: self._BOLD_RED,
        }[severity]

    def _fmt_details(self, details: Mapping[str, str]) -> str:
        if not details:
            return ""
        items = " ".join(f"{k}={v}" for k, v in details.items() if v != "")
        return f" {items}" if items else ""

    def _paint(self, text: str, code: str) -> str:
        if not self._color or not code:
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

    @classmethod
    def _truncate(cls, text: str) -> str:
        if len(text) <= cls._MAX_PREVIEW:
            return text
        return text[: cls._MAX_PREVIEW] + f"... (+{len(text) - cls._MAX_PREVIEW} chars)"
