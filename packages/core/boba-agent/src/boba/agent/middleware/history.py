"""
HistoryRecorderMiddleware: passthrough перехватчик, пишущий все AgentEvent в журнал
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import AgentEvent
from boba.agent.history import HistoryStoreError, HistoryWriter
from boba.patterns import StreamSource


class HistoryRecorderMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Регистрирует каждое AgentEvent из inner-стрима в HistoryService; passthrough.

    При HistoryStoreError эмитит PersistenceFailed (TerminalEvent) и завершает стрим;
    StopOnAnyFailure в StreamSourceLoop остановит агентский цикл.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        writer: HistoryWriter,
    ) -> None:
        self._inner = inner
        self._writer = writer

    def name(self) -> str:
        return "HistoryRecorder"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            try:
                self._writer.record(event)
                yield event
            except HistoryStoreError as err:
                # если ошибка записи, то просто отправляем event
                yield event
                # и генерируем PersistenceFailed, который
                # остановит агентский цикл через условие StopOnAnyFailure
                yield err.to_user_feedback(ctx.request_id)
