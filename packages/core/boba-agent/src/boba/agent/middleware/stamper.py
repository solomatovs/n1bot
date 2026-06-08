"""EventStamperMiddleware: проставляет envelope-поля seq / emitted_at / iteration.

Должен быть выставлен ближе к корню агентской цепочки, чтобы видеть ВСЕ
события: и из inner-middleware, и из HistoryRecorder. По умолчанию
подключается в AgentBuilder самым внешним слоем после HistoryRecorder
(см. AgentBuilder._build_chain).

Реализация passthrough: каждое событие из inner-стрима стампится в новую
копию и проксируется наружу. IterationStarted.iteration_count обновляет
текущее значение iteration-счётчика per-request, которое затем стампится
на всех последующих событиях этого request_id. Для PhaseEvent
дополнительно стампится duration_ns — дельта time.monotonic_ns()
с момента предыдущего PhaseEvent того же request_id.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime

from boba.agent.agent import AgentContext
from boba.agent.events import AgentEvent, IterationStarted, PhaseEvent
from boba.llm.models import RequestId
from boba.patterns import StreamSource


class EventStamperMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Стампит seq / emitted_at / iteration / duration_ns на каждом событии."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner
        self._seq_by_request: dict[RequestId, int] = {}
        self._iteration_by_request: dict[RequestId, int] = {}
        self._last_phase_mono_by_request: dict[RequestId, int] = {}

    def name(self) -> str:
        return "EventStamper"

    def reset(self) -> None:
        self._seq_by_request.clear()
        self._iteration_by_request.clear()
        self._last_phase_mono_by_request.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            if isinstance(event, IterationStarted):
                self._iteration_by_request[event.request_id] = event.iteration_count

            iteration = self._iteration_by_request.get(event.request_id, 0)
            seq = self._seq_by_request.get(event.request_id, 0) + 1
            self._seq_by_request[event.request_id] = seq

            update: dict[str, object] = {
                "seq": seq,
                "emitted_at": datetime.now(UTC),
                "iteration": iteration,
            }

            if isinstance(event, PhaseEvent):
                now_mono = time.monotonic_ns()
                last = self._last_phase_mono_by_request.get(event.request_id)
                update["duration_ns"] = now_mono - last if last is not None else 0
                self._last_phase_mono_by_request[event.request_id] = now_mono

            yield event.model_copy(update=update)
