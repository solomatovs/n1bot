"""Sink в stdout/stderr поверх категорий событий."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TextIO

from boba.agent.events import (
    AdvisoryEvent,
    AgentEvent,
    DeltaEvent,
    DiagnosticEvent,
    MessageEvent,
    PhaseEvent,
    Severity,
    StreamKind,
    TerminalEvent,
)


class ConsoleSink:
    """AgentEvent → stdout/stderr; use_color=None — авто по TTY/NO_COLOR.

    Флаги:
        `verbose`     - печатать `body` PhaseEvent'ов (раньше у
                        ToolExecutionStarted там лежал JSON args; сейчас
                        этим занимается diagnostic-режим).
        `diagnostic`  - показывать `DiagnosticEvent`-события (телеметрию).
                        По умолчанию False - «чистый» классический вывод.
    """

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

    _STREAMING_KINDS = frozenset(
        {
            StreamKind.ANSWER,
            StreamKind.THINKING,
            StreamKind.REFUSAL,
        }
    )

    _MAX_PREVIEW = 200

    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
        use_color: bool | None = None,
        verbose: bool = False,
        diagnostic: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._color = self._resolve_color(stdout, stderr, use_color)
        self._verbose = verbose
        self._diagnostic = diagnostic

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

    def handle(self, event: AgentEvent) -> None:
        match event:
            case DeltaEvent():
                self._on_delta(event)
            case MessageEvent():
                self._on_snapshot(event)
            case PhaseEvent():
                self._on_phase(event)
            case AdvisoryEvent():
                self._on_advisory(event)
            case TerminalEvent():
                self._on_terminal(event)
            case DiagnosticEvent():
                self._on_diagnostic(event)

    def _on_delta(self, e: DeltaEvent) -> None:
        if not e.chunk:
            return
        color = self._color_for_stream(e.stream_kind)
        self._inline(self._paint(e.chunk, color))

    def _on_snapshot(self, e: MessageEvent) -> None:
        if e.stream_kind in self._STREAMING_KINDS:
            self._line("")
            return
        color = self._color_for_stream(e.stream_kind)
        head_label = e.stream_kind.value
        if e.headline:
            head_label = f"{head_label}:{e.headline}"
        head = f"[{head_label}]"
        body = self._truncate(e.body)
        self._line(self._paint(f"{head} {body}", color))

    def _on_phase(self, e: PhaseEvent) -> None:
        color = self._color_for_severity(e.severity)
        details_str = self._fmt_details(e.details)
        duration_str = self._fmt_duration(e.duration_ns)
        self._line(self._paint(f"[{e.label}]{duration_str}{details_str}", color))
        if self._verbose and e.body:
            self._line(self._paint(self._truncate(e.body), self._DIM))

    _NS_PER_US = 1_000
    _NS_PER_MS = 1_000_000
    _NS_PER_S = 1_000_000_000

    @staticmethod
    def _fmt_duration(ns: int) -> str:
        if ns <= 0:
            return ""
        if ns < ConsoleSink._NS_PER_MS:
            return f" +{ns / ConsoleSink._NS_PER_US:.0f}µs"
        if ns < ConsoleSink._NS_PER_S:
            return f" +{ns / ConsoleSink._NS_PER_MS:.0f}ms"
        return f" +{ns / ConsoleSink._NS_PER_S:.2f}s"

    def _on_advisory(self, e: AdvisoryEvent) -> None:
        color = self._color_for_severity(e.severity)
        details_str = self._fmt_details(e.details)
        self._err(self._paint(f"[WARN] {e.headline}{details_str}", color))
        if e.body:
            self._err(self._paint(self._truncate(e.body), self._DIM))

    def _on_terminal(self, e: TerminalEvent) -> None:
        color = self._color_for_severity(e.severity)
        details_str = self._fmt_details(e.details)
        self._err(self._paint(f"[FATAL] {e.headline}{details_str}", color))
        if e.body:
            self._err(self._paint(self._truncate(e.body), self._DIM))

    def _on_diagnostic(self, e: DiagnosticEvent) -> None:
        if not self._diagnostic:
            return
        details_str = self._fmt_details(e.details)
        topic = e.topic or "diag"
        head = f"[diag:{topic}] {e.headline}{details_str}".rstrip()
        self._err(self._paint(head, self._DIM))
        if e.body:
            self._err(self._paint(self._truncate(e.body), self._DIM))

    def _color_for_stream(self, kind: StreamKind) -> str:
        return {
            StreamKind.ANSWER: "",
            StreamKind.THINKING: self._DIM,
            StreamKind.REFUSAL: self._YELLOW,
            StreamKind.USER_QUERY: self._BOLD_GREEN,
            StreamKind.FEEDBACK: self._BLUE,
            StreamKind.TOOL_INVOCATION: self._MAGENTA,
            StreamKind.TOOL_RESULT: self._GREEN,
        }[kind]

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
