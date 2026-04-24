"""Минимальный sink в stdout/stderr для S2.

Печатает AnswerToken'ы в stdout сразу, по мере поступления; новую
строку добавляет после GenerationDone; ошибки — в stderr с префиксом.
Никакой стилизации (цветов, иконок) — рендеринг UI добавим отдельным
sink'ом позже.
"""

from __future__ import annotations

import sys
from typing import TextIO

from boba.domain.core.patterns import StreamSink
from boba_2.domain.agent.events import (
    AgentEvent,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
)
from boba_2.domain.agent.models import AgentContext


class ConsoleSink(StreamSink[AgentContext, AgentEvent]):
    """AgentEvent → stdout/stderr.

    :attr:`stdout` / :attr:`stderr` принимают любые TextIO — удобно
    для тестов (``io.StringIO``).
    """

    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

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
