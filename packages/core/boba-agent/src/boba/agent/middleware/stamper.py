"""EventStamperMiddleware: проставляет envelope-поля seq / emitted_at / iteration.

Должен быть выставлен ближе к корню агентской цепочки, чтобы видеть ВСЕ
события: и из inner-middleware, и из HistoryRecorder. По умолчанию
подключается в AgentBuilder самым внешним слоем после HistoryRecorder
(см. AgentBuilder._build_chain).

Реализация passthrough: каждое событие из inner-стрима стампится в новую
копию и проксируется наружу. `IterationStarted.iteration_count` обновляет
текущее значение iteration-счётчика per-request, которое затем стампится
на всех последующих событиях этого request_id.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from boba.agent.events import AgentEvent, IterationStarted
from boba.agent.orchestrator import AgentContext
from boba.llm.models import RequestId
from boba.patterns import StreamSource


class EventStamperMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Стампит seq / emitted_at / iteration на каждом событии, проходящем сквозь."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner
        self._seq_by_request: dict[RequestId, int] = {}
        self._iteration_by_request: dict[RequestId, int] = {}

    def name(self) -> str:
        return "EventStamper"

    def reset(self) -> None:
        self._seq_by_request.clear()
        self._iteration_by_request.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            if isinstance(event, IterationStarted):
                self._iteration_by_request[event.request_id] = event.iteration_count

            iteration = self._iteration_by_request.get(event.request_id, 0)
            seq = self._seq_by_request.get(event.request_id, 0) + 1
            self._seq_by_request[event.request_id] = seq

            yield event.model_copy(update={
                "seq": seq,
                "emitted_at": datetime.now(UTC),
                "iteration": iteration,
            })
