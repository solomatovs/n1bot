"""Минимальный sink в stdout/stderr для S2.

Печатает AnswerToken'ы в stdout сразу, по мере поступления; новую
строку добавляет после GenerationDone; ошибки — в stderr с префиксом.
Никакой стилизации (цветов, иконок) — рендеринг UI добавим отдельным
sink'ом позже.
"""

from __future__ import annotations

from typing import TextIO

from boba.domain.core.patterns import StreamSink
from boba_2.domain.agent.events import (
    AgentEvent,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
)
from boba_2.domain.agent.models import AgentContext


class TextOutSink(StreamSink[AgentContext, AgentEvent]):
    """AgentEvent -> text out

    принимают любые TextIO — удобно для stdout/stderr и unit test
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

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:
        match event:
            case AnswerToken(token=t):
                self._stdout.write(t)
                self._stdout.flush()
            case GenerationDone(finish_reason=fr) if fr.is_terminal:
                # Newline только когда генерация реально завершилась —
                # не на промежуточном finish_reason=tool_calls между
                # итерациями.
                self._stdout.write("\n")
                self._stdout.flush()
            case GenerationFailed(error_kind=kind, message=msg):
                self._stderr.write(f"[{kind}] {msg}\n")
                self._stderr.flush()
